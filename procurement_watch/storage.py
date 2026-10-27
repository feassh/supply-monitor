from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3

from .models import Article, next_time, utcnow


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS keywords (
                keyword TEXT PRIMARY KEY COLLATE NOCASE CHECK(length(trim(keyword)) > 0)
            );
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT, next_run_at TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                url TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL,
                buyer TEXT NOT NULL, agency TEXT NOT NULL, notice_type TEXT NOT NULL,
                region TEXT NOT NULL, category TEXT NOT NULL, summary TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                UNIQUE(source_id, url)
            );
            CREATE INDEX IF NOT EXISTS articles_published ON articles(published_at DESC);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id),
                matched_keywords TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
                created_at TEXT NOT NULL, sent_at TEXT, last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                finished_at TEXT NOT NULL, status TEXT NOT NULL, fetched INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            PRAGMA user_version=1;
        """)

    def close(self):
        self.db.close()

    def bootstrap(self, cfg: dict):
        with self.db:
            for source in cfg["sources"]:
                self.db.execute("INSERT OR IGNORE INTO sources(id) VALUES (?)", (source["id"],))
            if self.setting("keywords_initialized") is None:
                for keyword in cfg.get("keywords", []):
                    self.db.execute("INSERT OR IGNORE INTO keywords VALUES (?)", (keyword.strip(),))
                self.db.execute("INSERT INTO settings VALUES ('keywords_initialized', 'true')")
            schedule = cfg.get("schedule", {"value": 1, "unit": "minutes"})
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('schedule', ?)", (json.dumps(schedule),))

    def setting(self, key: str):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key: str, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def set_schedule(self, value: int, unit: str):
        from .models import interval_seconds
        interval_seconds(value, unit)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('schedule', ?)",
                            (json.dumps({"value": value, "unit": unit}),))
            # 修改频率后下一轮立即生效，避免仍等待旧频率的截止时间。
            self.db.execute("UPDATE sources SET next_run_at=NULL")

    def keywords(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT keyword FROM keywords ORDER BY keyword")]

    def change_keyword(self, keyword: str, remove=False):
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("关键词不能为空")
        with self.db:
            if remove:
                self.db.execute("DELETE FROM keywords WHERE keyword=?", (keyword,))
            else:
                self.db.execute("INSERT OR IGNORE INTO keywords VALUES (?)", (keyword,))

    def due(self, source_id: str) -> bool:
        row = self.db.execute("SELECT next_run_at FROM sources WHERE id=?", (source_id,)).fetchone()
        return row[0] is None or row[0] <= utcnow().isoformat()

    def ingest(self, source_id: str, articles: list[Article], interval: int, notify_first=False) -> dict:
        """公告去重、首次基线、通知入队和成功状态在同一事务中提交。"""
        now = utcnow().isoformat()
        keywords = self.keywords()
        counts = {"fetched": len(articles), "new": 0, "queued": 0}
        with self.db:
            initialized = self.db.execute("SELECT initialized FROM sources WHERE id=?", (source_id,)).fetchone()[0]
            for article in articles:
                if article.source_id != source_id:
                    raise ValueError("公告来源与本轮来源不一致")
                exists = self.db.execute("SELECT id FROM articles WHERE source_id=? AND url=?",
                                         (source_id, article.url)).fetchone()
                fields = asdict(article)
                if exists:
                    self.db.execute("""UPDATE articles SET title=:title, published_at=:published_at,
                        buyer=:buyer, agency=:agency, notice_type=:notice_type, region=:region,
                        category=:category, summary=:summary, last_seen_at=:now
                        WHERE source_id=:source_id AND url=:url""", {**fields, "now": now})
                    continue
                columns = ",".join(fields)
                placeholders = ",".join(":" + key for key in fields)
                cursor = self.db.execute(f"INSERT INTO articles ({columns},first_seen_at,last_seen_at) "
                                         f"VALUES ({placeholders},:now,:now)", {**fields, "now": now})
                counts["new"] += 1
                matches = [k for k in keywords if k.casefold() in article.title.casefold()]
                if matches and (initialized or notify_first):
                    self.db.execute("""INSERT INTO outbox(article_id,matched_keywords,next_attempt_at,created_at)
                        VALUES (?,?,?,?)""", (cursor.lastrowid, json.dumps(matches, ensure_ascii=False), now, now))
                    counts["queued"] += 1
            self.db.execute("""UPDATE sources SET initialized=1,last_success_at=?,next_run_at=?,last_error=NULL
                WHERE id=?""", (now, next_time(interval), source_id))
            self.db.execute("""INSERT INTO crawl_runs(source_id,finished_at,status,fetched,new_count,queued)
                VALUES (?,?,'success',?,?,?)""", (source_id, now, counts["fetched"], counts["new"], counts["queued"]))
        return counts

    def record_failure(self, source_id: str, error: str, retry_seconds: int):
        with self.db:
            self.db.execute("UPDATE sources SET next_run_at=?,last_error=? WHERE id=?",
                            (next_time(retry_seconds), error, source_id))
            self.db.execute("INSERT INTO crawl_runs(source_id,finished_at,status,error) VALUES (?,?,'failed',?)",
                            (source_id, utcnow().isoformat(), error))

    def pending(self, limit=100, include_delayed=False):
        return self.db.execute("""SELECT o.id AS notification_id, o.matched_keywords, a.* FROM outbox o
            JOIN articles a ON a.id=o.article_id WHERE o.status='pending' AND (? OR o.next_attempt_at<=?)
            ORDER BY a.published_at DESC, o.id LIMIT ?""",
                               (include_delayed, utcnow().isoformat(), limit)).fetchall()

    def delivery_result(self, ids: list[int], error=None, retry_seconds=60):
        with self.db:
            for oid in ids:
                if error:
                    self.db.execute("""UPDATE outbox SET attempts=attempts+1,last_error=?,next_attempt_at=?
                        WHERE id=? AND status='pending'""", (error, next_time(retry_seconds), oid))
                else:
                    self.db.execute("""UPDATE outbox SET attempts=attempts+1,status='sent',sent_at=?,last_error=NULL
                        WHERE id=? AND status='pending'""", (utcnow().isoformat(), oid))

    def records(self, limit=20, keyword=None):
        if keyword:
            return self.db.execute("SELECT * FROM articles WHERE instr(lower(title),lower(?))>0 "
                                   "ORDER BY published_at DESC,id DESC LIMIT ?", (keyword, limit)).fetchall()
        return self.db.execute("SELECT * FROM articles ORDER BY published_at DESC,id DESC LIMIT ?", (limit,)).fetchall()

    def status(self):
        return {"sources": [dict(r) for r in self.db.execute("SELECT * FROM sources")],
                "articles": self.db.execute("SELECT count(*) FROM articles").fetchone()[0],
                "notifications": {r[0]: r[1] for r in self.db.execute("SELECT status,count(*) FROM outbox GROUP BY status")},
                "keywords": self.keywords(), "schedule": self.setting("schedule")}


@contextmanager
def worker_lock(database: str):
    """禁止两个工作进程同时比较基线或重复发送通知；进程退出即释放。"""
    path = Path(database + ".worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("另一个采集/推送工作进程正在运行") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
