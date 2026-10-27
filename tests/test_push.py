from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from procurement_watch.models import Article
from procurement_watch.notifications import QQWebhook, dispatch
from procurement_watch.push_config import effective_config, public_config, save_config
from procurement_watch.storage import Store
from procurement_watch.web import create_app, set_password


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "push.db")
        self.config = {"database":self.path, "sources":[{"id":"test","type":"ccgp","url":"https://example.com"}],
                       "keywords":["医院"], "schedule":{"value":30,"unit":"minutes"},
                       "notification":{"enabled":False,"url":"","group_id":"","token_env":"TEST_NAPCAT_TOKEN"}}
        self.defaults = self.config["notification"]
        self.store = Store(self.path)
        self.store.bootstrap(self.config)
        self.values = {"enabled":False,"url":"http://127.0.0.1:3000/send_group_msg", "group_id":"123456", "token":"synthetic-test-token"}
        set_password(self.store,"test-admin-password")
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.client.get("/login")
        self.assertEqual(self.post("/api/login",{"password":"test-admin-password"}).status_code,200)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def post(self,path,data):
        with self.client.session_transaction() as session:
            token = session["csrf"]
        return self.client.post(path,json=data,headers={"X-CSRF-Token":token})

    def test_encrypted_persistence_no_token_in_api_or_html(self):
        with patch("procurement_watch.web.QQWebhook.send") as sender:
            response = self.post("/api/push-config",self.values)
            sender.assert_not_called()
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json["has_token"])
        self.assertNotIn(self.values["token"],response.get_data(as_text=True))
        raw = self.store.db.execute("SELECT value FROM settings WHERE key='push_config'").fetchone()[0]
        self.assertNotIn(self.values["token"],raw)
        self.assertTrue(Path(self.path+".push.key").exists())
        reopened = Store(self.path)
        try:
            self.assertEqual(effective_config(reopened,self.defaults)["token"],self.values["token"])
        finally:
            reopened.close()
        self.assertNotIn(self.values["token"], self.client.get("/api/push-config").get_data(as_text=True))
        page = self.client.get("/push").get_data(as_text=True)
        self.assertIn("推送配置",page)
        self.assertNotIn(self.values["token"],page)

    def test_keep_replace_clear_token_and_environment(self):
        with patch.dict(os.environ,{"TEST_NAPCAT_TOKEN":"env-only-token"}):
            self.assertEqual(effective_config(self.store,self.defaults)["token"],"env-only-token")
            save_config(self.store,self.defaults,self.values)
            save_config(self.store,self.defaults,{**self.values,"token":"","group_id":"999"})
            self.assertEqual(effective_config(self.store,self.defaults)["token"],self.values["token"])
            save_config(self.store,self.defaults,{**self.values,"token":"replacement-test-token"})
            self.assertEqual(effective_config(self.store,self.defaults)["token"],"replacement-test-token")
            save_config(self.store,self.defaults,{**self.values,"token":"","clear_token":True})
            self.assertEqual(effective_config(self.store,self.defaults)["token"],"")
            self.assertFalse(public_config(self.store,self.defaults)["has_token"])

    def test_invalid_config_does_not_overwrite(self):
        self.post("/api/push-config",self.values)
        for invalid in ({"url":"file:///test"},{"url":"http://user:password@example.com"},
                        {"url":"http://localhost:bad/send_group_msg"},{"group_id":"not-a-group"},
                        {"token":"bad\nheader"},{"enabled":"yes"},{"enabled":True,"group_id":""},
                        {"token":"another","clear_token":True}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.post("/api/push-config",{**self.values,**invalid}).status_code,400)
        self.assertEqual(effective_config(self.store,self.defaults)["token"],self.values["token"])
        self.assertEqual(self.store.setting("push_config")["group_id"],"123456")

    def test_saved_config_used_by_dispatch_and_disable_preserves_queue(self):
        item = Article("test","https://example.com/1","医院采购公告","2026-10-27T18:00:00+08:00")
        self.store.ingest("test",[item],1800,True)
        save_config(self.store,self.defaults,self.values)
        with patch.object(QQWebhook,"send",autospec=True) as sender:
            self.assertEqual(dispatch(self.store,self.defaults),0)
            sender.assert_not_called()
            save_config(self.store,self.defaults,{**self.values,"enabled":True,"token":""})
            self.assertEqual(dispatch(self.store,self.defaults),1)
            self.assertEqual(sender.call_args.args[0].config["token"],self.values["token"])
            self.assertIn("医院采购公告",sender.call_args.args[1])
            self.assertTrue(self.client.get("/api/overview").json["notification_enabled"])

    def test_explicit_test_failure_success_and_queue_unchanged(self):
        self.post("/api/push-config",self.values)
        with patch.object(QQWebhook,"send",autospec=True) as sender:
            response = self.post("/api/push-test",{})
            self.assertEqual(response.status_code,200)
            self.assertIn("推送测试",sender.call_args.args[1])
            self.assertEqual(sender.call_args.args[0].config["group_id"],"123456")
        self.assertFalse(self.store.setting("push_config")["enabled"])
        self.assertTrue(self.client.get("/api/push-config").json["last_test"]["ok"])
        with patch.object(QQWebhook,"send",side_effect=RuntimeError("机器人未返回成功回执")):
            self.assertEqual(self.post("/api/push-test",{}).status_code,502)
        self.assertFalse(self.client.get("/api/push-config").json["last_test"]["ok"])
        self.assertEqual(self.store.status()["notifications"],{})
        self.post("/api/push-config",{**self.values,"token":"","group_id":"98765"})
        self.assertIsNone(self.client.get("/api/push-config").json["last_test"])

    def test_push_routes_require_login_and_csrf(self):
        self.post("/api/logout",{})
        self.assertEqual(self.client.get("/push").status_code,302)
        self.assertEqual(self.client.get("/api/push-config").status_code,401)
        self.client.get("/login")
        self.assertEqual(self.post("/api/push-config",self.values).status_code,401)
        self.assertEqual(self.post("/api/push-test",{}).status_code,401)
        self.assertEqual(self.client.post("/api/push-config",json=self.values).status_code,403)

    def test_missing_key_can_reconfigure_and_disabled_dispatch_is_quiet(self):
        save_config(self.store,self.defaults,self.values)
        Path(self.path+".push.key").unlink()
        self.assertTrue(public_config(self.store,self.defaults)["has_token"])
        with self.assertRaises(ValueError):
            effective_config(self.store,self.defaults)
        self.assertEqual(dispatch(self.store,self.defaults),0)
        save_config(self.store,self.defaults,{**self.values,"token":"recovered-test-token"})
        self.assertEqual(effective_config(self.store,self.defaults)["token"],"recovered-test-token")


class NapcatContractTests(unittest.TestCase):
    def test_http_bearer_and_payload_no_redirect(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path,self.headers.get("Authorization"),body))
                if self.path=="/redirect":
                    self.send_response(302)
                    self.send_header("Location","/unexpected")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok","retcode":0}')
            def log_message(self,*args):
                pass
        server=HTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            settings={"url":f"http://127.0.0.1:{server.server_port}/send_group_msg","group_id":"123456","token":"contract-test-token"}
            QQWebhook(settings).send("测试")
            self.assertEqual(requests[0][1],"Bearer contract-test-token")
            self.assertEqual(requests[0][2],{"message_type":"group","group_id":"123456","message":"测试","auto_escape":True})
            with self.assertRaises(RuntimeError):
                QQWebhook({**settings,"url":f"http://127.0.0.1:{server.server_port}/redirect"}).send("测试")
            self.assertEqual([r[0] for r in requests],["/send_group_msg","/redirect"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__=="__main__":
    unittest.main()
