from pathlib import Path
import tempfile
import unittest

from procurement_watch.models import Article
from procurement_watch.storage import Store
from procurement_watch.web import create_app, set_password


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"database": str(Path(self.temp.name) / "web.db"),
                       "sources": [{"id": "ccgp", "type": "ccgp", "url": "https://example.com", "enabled": True}],
                       "keywords": ["医院"], "schedule": {"value": 30, "unit": "minutes"}}
        self.store = Store(self.config["database"])
        self.store.bootstrap(self.config)
        self.store.ingest("ccgp", [Article("ccgp", "https://example.com/1", "医院采购公告", "2026-10-27T18:00:00+08:00"),
                                   Article("ccgp", "https://example.com/2", "学校采购公告", "2026-10-27T17:00:00+08:00")], 1800)
        self.app = create_app(self.config, allow_setup=True)
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.get("/login")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def post(self, url, data):
        with self.client.session_transaction() as state:
            token = state.get("csrf", "")
        return self.client.post(url, json=data, headers={"X-CSRF-Token":token})

    def login(self):
        result = self.post("/api/setup", {"password":"test-password-123"})
        self.assertEqual(result.status_code, 200)

    def test_authentication_setup_and_logout(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/api/overview").status_code, 401)
        self.assertEqual(self.client.get("/api/export").status_code, 401)
        self.assertEqual(self.post("/api/setup", {"password":"short"}).status_code, 400)
        self.login()
        page = self.client.get("/")
        self.assertIn("消毒供应采集系统", page.get_data(as_text=True))
        self.assertIn("HttpOnly", page.headers.get("Set-Cookie", ""))
        self.assertEqual(self.post("/api/setup", {"password":"another-password"}).status_code, 409)
        self.assertEqual(self.post("/api/logout", {}).status_code, 200)
        self.assertEqual(self.client.get("/api/articles").status_code, 401)
        self.client.get("/login")
        self.assertEqual(self.post("/api/login", {"password":"test-password-123"}).status_code, 200)

    def test_csrf_and_password_reset_invalidate_session(self):
        self.login()
        self.assertEqual(self.client.post("/api/pause", json={"paused":True}).status_code, 403)
        with self.client.session_transaction() as state:
            csrf = state["csrf"]
        self.assertEqual(self.client.post("/api/pause", json={"paused":True}, headers={"X-CSRF-Token":csrf,"Origin":"https://other.example"}).status_code, 403)
        set_password(self.store, "changed-password-456")
        self.assertEqual(self.client.get("/api/overview").status_code, 401)

    def test_login_rate_limit(self):
        self.login()
        self.post("/api/logout", {})
        self.client.get("/login")
        for _ in range(5):
            self.assertEqual(self.post("/api/login", {"password":"wrong"}).status_code, 401)
        self.assertEqual(self.post("/api/login", {"password":"test-password-123"}).status_code, 429)

    def test_management_persistence_and_filtering(self):
        self.login()
        self.assertEqual(self.client.get("/api/articles").json["total"], 2)
        self.assertEqual(self.client.get("/api/articles?matched=1").json["total"], 1)
        self.assertEqual(self.client.get("/api/articles?q=学校").json["items"][0]["title"], "学校采购公告")
        self.assertEqual(self.post("/api/keywords", {"action":"add","keyword":"学校"}).status_code, 200)
        self.assertEqual(self.client.get("/api/articles?matched=1").json["total"], 2)
        self.assertEqual(self.post("/api/keywords", {"action":"remove","keyword":"医院"}).status_code, 200)
        self.assertEqual(self.post("/api/schedule", {"value":2,"unit":"hours"}).status_code, 200)
        self.assertEqual(self.post("/api/pause", {"paused":True}).status_code, 200)
        overview = self.client.get("/api/overview").json
        self.assertTrue(overview["paused"])
        self.assertEqual(overview["schedule"], {"value":2,"unit":"hours"})
        self.assertEqual(self.store.keywords(), ["学校"])
        exported = self.client.get("/api/export?q=学校")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("学校采购公告", exported.get_data(as_text=True))
        self.assertNotIn("医院采购公告", exported.get_data(as_text=True))

    def test_invalid_inputs_and_remote_setup(self):
        self.login()
        for path, data in [("/api/schedule", {"value":0,"unit":"minutes"}),
                           ("/api/schedule", {"value":2,"unit":"bad"}),
                           ("/api/keywords", {"action":"add","keyword":" "}),
                           ("/api/keywords", {"action":"bad","keyword":"word"}),
                           ("/api/pause", {"paused":"yes"})]:
            self.assertEqual(self.post(path, data).status_code, 400)
        self.assertEqual(self.post("/api/collect", {}).status_code, 503)
        self.assertEqual(self.client.get("/api/articles?page=bad").status_code, 400)
        self.assertEqual(self.client.get("/login", headers={"Host":"evil.example"}).status_code, 400)

    def test_background_collect_trigger(self):
        class Worker:
            running = False
            manual = False
            def trigger(self):
                if self.manual:
                    return False
                self.manual = True
                return True
        app = create_app(self.config, worker=Worker(), allow_setup=True)
        self.client = app.test_client()
        self.client.get("/login")
        self.login()
        self.assertEqual(self.post("/api/collect", {}).status_code, 202)
        self.assertEqual(self.post("/api/collect", {}).status_code, 409)
        self.assertTrue(self.client.get("/api/overview").json["running"])

    def test_trusted_proxy_origin(self):
        set_password(self.store, "proxy-password-123")
        app = create_app(self.config, trust_proxy=True)
        client = app.test_client()
        client.get("/login", headers={"X-Forwarded-Proto":"https"})
        with client.session_transaction() as state:
            csrf = state["csrf"]
        response = client.post("/api/login", json={"password":"proxy-password-123"},
                               headers={"X-CSRF-Token":csrf,"Origin":"https://localhost","X-Forwarded-Proto":"https"})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
