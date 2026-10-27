from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from procurement_watch.collectors.ccgp import CcgpCollector
from procurement_watch.config import load_config
from procurement_watch.http import FetchError
from procurement_watch.models import Article, interval_seconds
from procurement_watch.notifications import QQWebhook, dispatch, message_batches
from procurement_watch.service import WatchService
from procurement_watch.storage import Store, worker_lock

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "website-1.html").read_text(encoding="utf-8")
SOURCE = {"id": "test", "type": "ccgp", "url": "https://search.ccgp.gov.cn/bxsearch?zoneId=33+not+3302&kw=&page_index=1"}


def article(url="https://example.com/1", title="医院采购公告"):
    return Article("test", url, title, "2026-10-27T18:05:12+08:00", "采购人", "代理机构", "公开招标公告", "浙江")


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.collector = CcgpCollector(SOURCE, None, {})

    def test_real_saved_html(self):
        parsed = self.collector.parse(HTML, SOURCE["url"])
        self.assertEqual(len(parsed.articles), 20)
        self.assertEqual(parsed.total_pages, 1637)
        item = next(a for a in parsed.articles if "27571974" in a.url)
        self.assertEqual(item.title, "浙江大学医学院附属儿童医院电子内窥镜系统（胃肠镜）公开招标公告")
        self.assertEqual(item.published_at, "2026-10-27T18:05:12+08:00")
        self.assertEqual(item.buyer, "浙江大学医学院附属儿童医院")
        self.assertEqual(item.agency, "浙江国际招投标有限公司")
        self.assertEqual(item.notice_type, "公开招标公告")
        self.assertEqual(item.region, "浙江")
        self.assertEqual(item.category, "")

    def test_blocked_empty_changed_malformed(self):
        for html in ("您的访问过于频繁", "<html>登录后查看</html>",
                     '<ul class="vT-srch-result-list-bid"><li><a href="/a">标题</a></li></ul>'):
            with self.subTest(html=html), self.assertRaises(FetchError):
                self.collector.parse(html, SOURCE["url"])
        self.assertEqual(self.collector.parse("共找到 <span>0</span> 条内容", SOURCE["url"]).articles, [])

    def test_query_preserves_region_and_fixed_dates(self):
        url = self.collector.page_url()
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        self.assertEqual(query["zoneId"], ["33 not 3302"])
        self.assertEqual(query["page_index"], ["1"])
        self.assertEqual(query["kw"], [""])
        cfg = load_config(str(ROOT / "config.example.toml"))
        source = {**cfg["sources"][0], "date_mode": "fixed"}
        collector = CcgpCollector(source, None, {})
        self.assertEqual(parse_qs(urlsplit(collector.page_url()).query)["end_time"], ["2026:10:26"])

    def test_only_first_page_even_with_pager_and_legacy_config(self):
        class Client:
            def __init__(self):
                self.urls = []

            def get(self, url):
                self.urls.append(url)
                return HTML
        client = Client()
        source = {**SOURCE, "max_pages": 100, "url": SOURCE["url"].replace("page_index=1", "page_index=9")}
        collector = CcgpCollector(source, client, {})
        self.assertEqual(len(collector.collect()), 20)
        self.assertEqual(len(client.urls), 1)
        self.assertEqual(parse_qs(urlsplit(client.urls[0]).query)["page_index"], ["1"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "db.sqlite")
        self.store = Store(self.path)
        self.cfg = {"sources": [SOURCE], "keywords": ["医院", "采购"],
                    "schedule": {"value": 30, "unit": "minutes"}}
        self.store.bootstrap(self.cfg)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_baseline_difference_matching_restart(self):
        first = article()
        self.assertEqual(self.store.ingest("test", [first], 60), {"fetched": 1, "new": 1, "queued": 0})
        self.store.close()
        self.store = Store(self.path)
        self.store.bootstrap(self.cfg)
        self.assertEqual(len(self.store.records()), 1)
        result = self.store.ingest("test", [replace(first, title="医院采购修改"),
                                           article("https://example.com/2"),
                                           article("https://example.com/3", "学校通知")], 60)
        self.assertEqual(result, {"fetched": 3, "new": 2, "queued": 1})
        pending = self.store.pending()
        self.assertEqual(json.loads(pending[0]["matched_keywords"]), ["医院", "采购"])
        self.assertEqual(self.store.ingest("test", [article("https://example.com/2")], 60)["queued"], 0)

    def test_keywords_schedule_persist(self):
        self.store.change_keyword("新词")
        self.store.change_keyword("医院", remove=True)
        self.store.set_schedule(2, "hours")
        self.store.bootstrap(self.cfg)
        self.assertEqual(self.store.keywords(), ["新词", "采购"])
        self.assertEqual(self.store.setting("schedule"), {"value": 2, "unit": "hours"})
        self.assertEqual(interval_seconds(2, "days"), 172800)
        with self.assertRaises(ValueError):
            self.store.set_schedule(0, "minutes")

    def test_atomic_rollback(self):
        with self.assertRaises(ValueError):
            self.store.ingest("test", [article(), replace(article(), source_id="wrong")], 60, True)
        self.assertEqual(self.store.status()["articles"], 0)
        self.assertEqual(self.store.status()["sources"][0]["initialized"], 0)

    def test_failure_does_not_create_baseline(self):
        class BadClient:
            def get(self, url):
                raise FetchError("访问被限制")
        service = WatchService(self.store, self.cfg, client=BadClient())
        with self.assertLogs(level="ERROR"):
            self.assertFalse(service.collect())
        status = self.store.status()
        self.assertEqual(status["sources"][0]["initialized"], 0)
        self.assertIsNone(status["sources"][0]["last_success_at"])
        self.assertFalse(self.store.due("test"))
        self.assertEqual(status["articles"], 0)

    def test_notify_failure_retry_disable_and_preview(self):
        self.store.ingest("test", [article()], 60, True)
        class Sender:
            fail = True
            def send(self, text):
                if self.fail:
                    raise RuntimeError("机器人离线")
                self.text = text
        sender = Sender()
        self.assertEqual(dispatch(self.store, {}, sender), 0)
        with self.assertLogs(level="ERROR"):
            self.assertEqual(dispatch(self.store, {"enabled": True}, sender), 0)
        row = self.store.db.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(len(self.store.pending()), 0)
        sender.fail = False
        self.store.db.execute("UPDATE outbox SET next_attempt_at='2000'")
        self.store.db.commit()
        self.assertEqual(dispatch(self.store, {"enabled": True}, sender), 1)
        self.assertIn("https://example.com/1", sender.text)
        self.assertEqual(dispatch(self.store, {"enabled": True}, sender), 0)

    def test_message_chunks_no_item_loss(self):
        self.store.ingest("test", [article(f"https://example.com/{i}") for i in range(12)], 60, True)
        batches = list(message_batches(self.store.pending(), 5, 3500))
        self.assertEqual([len(ids) for ids, _ in batches], [5, 5, 2])
        self.assertEqual(len({i for ids, _ in batches for i in ids}), 12)
        with self.assertRaises(ValueError):
            list(message_batches(self.store.pending(), 5, 10))

    def test_process_lock(self):
        with worker_lock(self.path):
            with self.assertRaises(RuntimeError):
                with worker_lock(self.path):
                    pass
        with worker_lock(self.path):
            pass


class WebhookTests(unittest.TestCase):
    def test_http_contract_and_business_failure(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "retcode": 0} if len(received) == 1 else
                                            {"status": "failed", "retcode": 100}).encode())
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sender = QQWebhook({"url": f"http://127.0.0.1:{server.server_port}/send_group_msg", "group_id": "123"})
            sender.send("中文标题\nhttps://example.com")
            self.assertEqual(received[0], {"message_type": "group", "group_id": "123", "message": "中文标题\nhttps://example.com", "auto_escape": True})
            with self.assertRaises(RuntimeError):
                sender.send("错误回执")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
