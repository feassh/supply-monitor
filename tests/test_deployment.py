from pathlib import Path
from http.cookiejar import CookieJar
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from procurement_watch.config import load_config
from procurement_watch.storage import Store
from procurement_watch.web import create_app
from scripts.render_hub_compose import render


class DeploymentTests(unittest.TestCase):
    def test_hub_compose_has_real_image_without_build_or_parameters(self):
        output = render("testuser/supply-monitor:1.2.3")
        self.assertIn("image: testuser/supply-monitor:1.2.3",output)
        self.assertNotIn("build:",output)
        self.assertNotIn("${",output)
        self.assertIn("monitor-data:/app/data",output)
        self.assertIn('"8000:8000"',output)
        with self.assertRaises(ValueError):
            render("bad\nimage: invalid")

    def test_default_one_minute_and_preserve_saved_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = Path(directory) / "config.toml"
            cfg_path.write_text('[[sources]]\nid="test"\ntype="ccgp"\nurl="https://example.com"\n', encoding="utf-8")
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg["schedule"], {"value":1,"unit":"minutes"})
            store = Store(cfg["database"])
            try:
                store.bootstrap(cfg)
                self.assertEqual(store.setting("schedule"), {"value":1,"unit":"minutes"})
                store.set_schedule(2,"hours")
                store.bootstrap(cfg)
                self.assertEqual(store.setting("schedule"), {"value":2,"unit":"hours"})
            finally:
                store.close()

    def test_remote_first_setup_requires_one_time_code(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"database":str(Path(directory)/"test.db"),
                   "sources":[{"id":"test","type":"ccgp","url":"https://example.com"}]}
            app = create_app(cfg, setup_token="synthetic-initialization-code")
            client = app.test_client()
            response = client.get("/login", environ_overrides={"REMOTE_ADDR":"203.0.113.10"})
            self.assertIn('id="setup-token"',response.get_data(as_text=True))
            self.assertNotIn("synthetic-initialization-code",response.get_data(as_text=True))
            with client.session_transaction() as state:
                csrf = state["csrf"]
            def setup(code):
                return client.post("/api/setup",json={"password":"test-admin-password","setup_token":code},
                                   headers={"X-CSRF-Token":csrf},environ_overrides={"REMOTE_ADDR":"203.0.113.10"})
            self.assertEqual(setup("").status_code,403)
            self.assertEqual(setup("错误的验证码").status_code,403)
            self.assertEqual(setup("synthetic-initialization-code").status_code,200)
            with client.session_transaction() as state:
                csrf = state["csrf"]
            self.assertEqual(setup("synthetic-initialization-code").status_code,409)
            self.assertEqual(client.get("/healthz").json,{"status":"ok"})
            response = client.get("/api/overview")
            self.assertEqual(response.json["schedule"],{"value":1,"unit":"minutes"})

    def test_production_server_starts_and_serves_setup_and_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory)/"config.toml"
            config_path.write_text('[[sources]]\nid="test"\ntype="ccgp"\nurl="https://example.com"\nenabled=false\n',encoding="utf-8")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1",0))
                port = listener.getsockname()[1]
            code = ("import logging,sys; from procurement_watch.config import load_config; "
                    "from procurement_watch.web import serve; logging.basicConfig(level=logging.INFO); "
                    "serve(load_config(sys.argv[1]),host='127.0.0.1',port=int(sys.argv[2]),production=True,initial_setup=True)")
            with (Path(directory)/"server.log").open("w+",encoding="utf-8") as log:
                env = {**os.environ,"PYTHONIOENCODING":"utf-8","PYTHONUTF8":"1"}
                # Windows venv 的启动器可能创建子进程，直接运行解释器才能可靠回收测试服务。
                executable = str(Path(sys.base_prefix)/"python.exe") if os.name == "nt" else sys.executable
                env["PYTHONPATH"] = os.pathsep.join(str(p) for p in sys.path if p)
                process = subprocess.Popen([executable,"-c",code,str(config_path),str(port)],
                                           stdout=log,stderr=subprocess.STDOUT,env=env)
                try:
                    base = f"http://127.0.0.1:{port}"
                    client = build_opener(HTTPCookieProcessor(CookieJar()))
                    for _ in range(100):
                        self.assertIsNone(process.poll(),"生产服务器提前退出")
                        try:
                            with client.open(base+"/healthz",timeout=1) as response:
                                self.assertEqual(json.load(response),{"status":"ok"})
                            break
                        except (URLError,OSError):
                            time.sleep(.1)
                    else:
                        self.fail("生产服务器启动超时")
                    with client.open(base+"/login",timeout=3) as response:
                        html = response.read().decode()
                    self.assertIn('id="setup-token"',html)
                    csrf = re.search(r'name="csrf-token" content="([^"]+)"',html).group(1)
                    log.seek(0)
                    setup_code = re.search(r"首次初始化验证码[^：]*：([^\s]+)",log.read()).group(1)
                    self.assertNotIn(setup_code,html)
                    request = Request(base+"/api/setup", data=json.dumps({"password":"runtime-test-password","setup_token":setup_code}).encode(),
                                      headers={"Content-Type":"application/json","X-CSRF-Token":csrf,"Origin":base},method="POST")
                    with client.open(request,timeout=5) as response:
                        self.assertTrue(json.load(response)["ok"])
                    with client.open(base+"/api/overview",timeout=3) as response:
                        self.assertEqual(json.load(response)["schedule"],{"value":1,"unit":"minutes"})
                    for asset in ("app.css","app.js"):
                        with client.open(base+"/static/"+asset,timeout=3) as response:
                            self.assertEqual(response.status,200)
                finally:
                    process.send_signal(signal.SIGTERM) if process.poll() is None else None
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
