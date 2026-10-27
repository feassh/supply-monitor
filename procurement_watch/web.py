"""本地管理界面、受保护的 JSON API 和单进程后台工作线程。"""
from datetime import timedelta
from functools import wraps
import csv
import io
import logging
import secrets
import signal
import sqlite3
import threading
import time

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from .models import utcnow
from .notifications import QQWebhook
from .push_config import effective_config, public_config, save_config
from .service import WatchService
from .storage import Store, worker_lock

log = logging.getLogger(__name__)


def set_password(store, password):
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise ValueError("密码长度应为 10 至 256 个字符")
    store.set_setting("web_password", generate_password_hash(password, method="pbkdf2:sha256:600000"))
    store.set_setting("web_auth_version", secrets.token_hex(16))


class WebWorker:
    def __init__(self, config, wait_for_setup=False):
        self.config = config
        self.wait_for_setup = wait_for_setup
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.guard = threading.Lock()
        self.running = False
        self.manual = False
        self.thread = threading.Thread(target=self._run, name="collection-worker", daemon=True)

    def trigger(self):
        with self.guard:
            if self.running or self.manual:
                return False
            self.manual = True
        self.wake.set()
        return True

    def _run(self):
        store = Store(self.config["database"])
        service = WatchService(store, self.config)
        try:
            while not self.stop.is_set():
                with self.guard:
                    manual = self.manual
                    self.manual = False
                    ready = not self.wait_for_setup or bool(store.setting("web_password"))
                    due = ready and not store.setting("collection_paused") and any(
                        s.get("enabled", True) and store.due(s["id"]) for s in self.config["sources"])
                    self.running = manual or due
                try:
                    if self.running:
                        service.collect(due_only=not manual)
                    if ready and not self.stop.is_set():
                        service.notify(self.stop)
                except Exception:
                    log.exception("后台任务异常")
                finally:
                    with self.guard:
                        self.running = False
                self.wake.wait(5)
                self.wake.clear()
        finally:
            store.close()


def create_app(config, worker=None, allow_setup=False, secure_cookie=False, trust_proxy=False, setup_token=None):
    app = Flask(__name__)
    if trust_proxy:
        # 只信任明确启用的一层代理协议头，不信任转发的客户端 IP 或 Host。
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0)
    initial = Store(config["database"])
    initial.bootstrap(config)
    secret = initial.setting("web_secret")
    if not secret:
        secret = secrets.token_hex(32)
        initial.set_setting("web_secret", secret)
    initial.db.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
        ip TEXT PRIMARY KEY, failures INTEGER NOT NULL, locked_until REAL NOT NULL)""")
    initial.db.commit()
    initial.close()
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=secure_cookie,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=16_384)
    if allow_setup and not setup_token:
        app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1", "[::1]"]

    def store():
        if "store" not in g:
            g.store = Store(config["database"])
        return g.store

    def authenticated():
        version = store().setting("web_auth_version")
        return bool(version and session.get("auth") == version)

    def protected(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not authenticated():
                return jsonify(error="请先登录"), 401
            return fn(*args, **kwargs)
        return wrapped

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("请求数据格式无效")
        return data

    @app.before_request
    def security():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            expected = session.get("csrf", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="请求来源无效"), 403
            if not expected or not secrets.compare_digest(expected, supplied):
                return jsonify(error="页面已过期，请刷新后重试"), 403

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return response

    @app.teardown_appcontext
    def close_store(error):
        instance = g.pop("store", None)
        if instance:
            instance.close()

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(sqlite3.Error)
    def database_error(exc):
        log.exception("管理接口数据库异常")
        return jsonify(error="数据库暂时不可用，请稍后重试"), 503

    @app.get("/")
    def index():
        if not authenticated():
            return redirect("/login")
        session.setdefault("csrf", secrets.token_hex(32))
        return render_template("dashboard.html", csrf=session["csrf"])

    @app.get("/healthz")
    def health():
        store().db.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    @app.get("/login")
    def login_page():
        if authenticated():
            return redirect("/")
        session.setdefault("csrf", secrets.token_hex(32))
        setup = not store().setting("web_password")
        return render_template("login.html", csrf=session["csrf"], setup=setup,
                               setup_token_required=bool(setup_token),
                               setup_allowed=bool(setup_token) or (allow_setup and request.remote_addr in ("127.0.0.1", "::1")))

    def login_session():
        session.clear()
        session.permanent = True
        session["auth"] = store().setting("web_auth_version")
        session["csrf"] = secrets.token_hex(32)

    @app.get("/push")
    def push_page():
        if not authenticated():
            return redirect("/login")
        session.setdefault("csrf", secrets.token_hex(32))
        return render_template("push.html", csrf=session["csrf"])

    @app.post("/api/setup")
    def setup():
        if setup_token:
            supplied = payload().get("setup_token", "")
            if not isinstance(supplied, str) or not supplied.isascii() or not secrets.compare_digest(setup_token, supplied):
                return jsonify(error="初始化验证码不正确，请查看容器启动日志"), 403
        elif not allow_setup or request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify(error="请在服务器终端运行 web-password 设置密码"), 403
        # 同一进程内串行化首次密码设置，避免并发覆盖。
        with setup_guard:
            if store().setting("web_password"):
                return jsonify(error="密码已设置，请使用登录入口"), 409
            set_password(store(), payload().get("password"))
        login_session()
        return jsonify(ok=True)

    setup_guard = threading.Lock()

    @app.post("/api/login")
    def login():
        ip = request.remote_addr or "unknown"
        now = time.time()
        row = store().db.execute("SELECT * FROM login_attempts WHERE ip=?", (ip,)).fetchone()
        if row and row["locked_until"] > now:
            return jsonify(error="尝试次数过多，请 15 分钟后重试"), 429
        password = payload().get("password", "")
        hashed = store().setting("web_password")
        if not isinstance(password, str) or len(password) > 256 or not hashed or not check_password_hash(hashed, password):
            failures = (row["failures"] if row and row["locked_until"] == 0 else 0) + 1
            with store().db:
                store().db.execute("INSERT OR REPLACE INTO login_attempts VALUES (?,?,?)",
                                   (ip, failures, now + 900 if failures >= 5 else 0))
            return jsonify(error="密码不正确，请重试"), 401
        with store().db:
            store().db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
        login_session()
        return jsonify(ok=True)

    @app.post("/api/logout")
    @protected
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/overview")
    @protected
    def overview():
        data = store().status()
        by_id = {s["id"]: s for s in config["sources"]}
        for source in data["sources"]:
            spec = by_id.get(source["id"], {})
            source.update(name=spec.get("name", "中国政府采购网" if spec.get("type") == "ccgp" else source["id"]),
                          enabled=spec.get("enabled", False), url=spec.get("url", ""))
        data.update(paused=bool(store().setting("collection_paused")),
                    running=bool(worker and (worker.running or worker.manual)), worker_online=worker is not None,
                    notification_enabled=public_config(store(), config.get("notification", {}))["enabled"])
        data["recent_runs"] = [dict(r) for r in store().db.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 20")]
        return jsonify(data)

    def article_query():
        search = request.args.get("q", "").strip()[:200]
        source = request.args.get("source", "")
        matched = request.args.get("matched") == "1"
        clauses, params = [], []
        if search:
            clauses.append("instr(lower(a.title), lower(?))>0")
            params.append(search)
        if source:
            clauses.append("a.source_id=?")
            params.append(source)
        keywords = store().keywords()
        if matched:
            if keywords:
                clauses.append("(" + " OR ".join("instr(lower(a.title),lower(?))>0" for _ in keywords) + ")")
                params.extend(keywords)
            else:
                clauses.append("0")
        return " WHERE " + " AND ".join(clauses) if clauses else "", params, keywords

    @app.get("/api/articles")
    @protected
    def articles():
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            raise ValueError("页码无效")
        where, params, keywords = article_query()
        total = store().db.execute("SELECT count(*) FROM articles a" + where, params).fetchone()[0]
        rows = store().db.execute("SELECT a.*,o.status AS notification_status FROM articles a "
                                 "LEFT JOIN outbox o ON o.article_id=a.id" + where +
                                 " ORDER BY a.published_at DESC,a.id DESC LIMIT 10 OFFSET ?", [*params, (page - 1) * 10])
        items = []
        for row in rows:
            item = dict(row)
            item["matched_keywords"] = [k for k in keywords if k.casefold() in item["title"].casefold()]
            items.append(item)
        return jsonify(items=items, total=total, page=page, page_size=10)

    @app.get("/api/export")
    @protected
    def export():
        where, params, _ = article_query()
        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(["标题", "发布时间", "采购人", "代理机构", "公告类型", "地区", "链接"])
        for row in store().db.execute("SELECT a.* FROM articles a" + where + " ORDER BY published_at DESC", params):
            # 避免表格软件将外部公告标题当作公式执行。
            writer.writerow([("'" + str(row[k])) if str(row[k]).startswith(("=", "+", "-", "@", "\t", "\r")) else row[k]
                             for k in ("title", "published_at", "buyer", "agency", "notice_type", "region", "url")])
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=announcements.csv"})

    @app.post("/api/keywords")
    @protected
    def keywords():
        data = payload()
        keyword = data.get("keyword")
        if not isinstance(keyword, str) or len(keyword) > 80:
            raise ValueError("关键词应为不超过 80 字的文本")
        if data.get("action") not in ("add", "remove"):
            raise ValueError("关键词操作无效")
        if data["action"] == "add" and len(store().keywords()) >= 50:
            raise ValueError("最多设置 50 个关键词")
        store().change_keyword(keyword, remove=data["action"] == "remove")
        return jsonify(keywords=store().keywords())

    @app.post("/api/schedule")
    @protected
    def schedule():
        data = payload()
        value, unit = data.get("value"), data.get("unit")
        if not isinstance(value, int) or isinstance(value, bool) or value > 365:
            raise ValueError("采集间隔应为 1 至 365 的整数")
        store().set_schedule(value, unit)
        return jsonify(ok=True)

    @app.post("/api/pause")
    @protected
    def pause():
        paused = payload().get("paused")
        if not isinstance(paused, bool):
            raise ValueError("任务状态无效")
        store().set_setting("collection_paused", paused)
        return jsonify(ok=True)

    @app.post("/api/collect")
    @protected
    def collect():
        if worker is None:
            return jsonify(error="采集工作进程未启动"), 503
        if not any(s.get("enabled", True) for s in config["sources"]):
            return jsonify(error="当前没有启用的采集来源"), 400
        if not worker.trigger():
            return jsonify(error="采集正在进行，请稍后查看结果"), 409
        return jsonify(ok=True), 202

    @app.get("/api/push-config")
    @protected
    def get_push_config():
        return jsonify(public_config(store(), config.get("notification", {})))

    @app.post("/api/push-config")
    @protected
    def update_push_config():
        return jsonify(save_config(store(), config.get("notification", {}), payload()))

    test_guard = threading.Lock()

    @app.post("/api/push-test")
    @protected
    def test_push():
        if not test_guard.acquire(blocking=False):
            return jsonify(error="测试消息正在发送，请稍后查看结果"), 409
        try:
            settings = effective_config(store(), config.get("notification", {}))
            if not settings.get("url") or not settings.get("group_id"):
                raise ValueError("请先保存机器人接口 URL 和群组 ID")
            try:
                QQWebhook(settings).send("消毒供应采集系统 · 推送测试\n这是一条测试消息，机器人群消息推送连接正常。")
            except (RuntimeError, ValueError) as exc:
                result = {"ok": False, "at": utcnow().isoformat(), "error": str(exc)}
                store().set_setting("push_last_test", result)
                return jsonify(error=str(exc), last_test=result), 502
            result = {"ok": True, "at": utcnow().isoformat()}
            store().set_setting("push_last_test", result)
            return jsonify(ok=True, last_test=result)
        finally:
            test_guard.release()

    return app


def serve(config, host="127.0.0.1", port=8000, secure_cookie=False, trust_proxy=False,
          production=False, initial_setup=False):
    local = host in ("127.0.0.1", "localhost", "::1")
    store = Store(config["database"])
    try:
        needs_password = not store.setting("web_password")
        if (not local or trust_proxy) and needs_password and not initial_setup:
            raise ValueError("对外监听前，请先运行 web-password 设置管理密码")
    finally:
        store.close()
    with worker_lock(config["database"]):
        setup_token = secrets.token_urlsafe(24) if initial_setup and needs_password else None
        worker = WebWorker(config, wait_for_setup=initial_setup)
        app = create_app(config, worker, allow_setup=local and not trust_proxy,
                         secure_cookie=secure_cookie, trust_proxy=trust_proxy, setup_token=setup_token)
        if setup_token:
            log.warning("首次初始化验证码（仅用于 Web 创建管理密码）：%s", setup_token)
        worker.thread.start()
        previous_signals = {}
        def shutdown(signum, frame):
            worker.stop.set()
            worker.wake.set()
            raise KeyboardInterrupt
        try:
            log.info("管理界面：http://%s:%s", host, port)
            if production:
                from waitress import serve as serve_wsgi
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous_signals[sig] = signal.signal(sig, shutdown)
                serve_wsgi(app, host=host, port=port, threads=4, max_request_body_size=16_384,
                           channel_timeout=30, expose_tracebacks=False)
            else:
                app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        except KeyboardInterrupt:
            log.info("正在停止 Web 服务与采集任务")
        finally:
            worker.stop.set()
            worker.wake.set()
            worker.thread.join()
            for sig, handler in previous_signals.items():
                signal.signal(sig, handler)
