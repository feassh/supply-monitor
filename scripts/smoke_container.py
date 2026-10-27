"""在 CI 中使用临时 Docker 数据卷验证真正的发布镜像，不连接生产站点。"""
from http.cookiejar import CookieJar
import json
import re
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def main(image):
    name = "supply-monitor-smoke-" + secrets.token_hex(4)
    volume = name + "-data"
    password = secrets.token_urlsafe(24)
    docker("volume", "create", volume)
    try:
        # 只暂停测试数据库的采集，保持镜像与真正部署完全相同。
        docker("run", "--rm", "--mount", f"source={volume},target=/app/data", "--entrypoint", "python", image,
               "-c", "from procurement_watch.config import load_config; from procurement_watch.storage import Store; "
               "c=load_config('/app/config.toml'); s=Store(c['database']); s.bootstrap(c); "
               "s.set_setting('collection_paused',True); s.close()")
        docker("run", "-d", "--name", name, "--mount", f"source={volume},target=/app/data",
               "-p", "127.0.0.1::8000", image)
        mapping = json.loads(docker("inspect", name).stdout)[0]["NetworkSettings"]["Ports"]["8000/tcp"][0]
        base = "http://127.0.0.1:" + mapping["HostPort"]

        def wait_ready():
            for _ in range(60):
                try:
                    with build_opener().open(base + "/healthz", timeout=2) as response:
                        if response.status == 200:
                            return
                except (URLError, OSError):
                    time.sleep(1)
            raise AssertionError("容器未能提供健康检查接口")

        def browser():
            return build_opener(HTTPCookieProcessor(CookieJar()))

        def page(client):
            with client.open(base + "/login", timeout=5) as response:
                html = response.read().decode()
            token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
            return html, token

        def post(client, route, csrf, data):
            request = Request(base + route, data=json.dumps(data).encode(),
                              headers={"Content-Type":"application/json","X-CSRF-Token":csrf,"Origin":base}, method="POST")
            with client.open(request, timeout=5) as response:
                return json.load(response)

        wait_ready()
        client = browser()
        try:
            client.open(base + "/api/overview", timeout=5)
            raise AssertionError("未登录时数据接口意外开放")
        except HTTPError as exc:
            assert exc.code == 401
            exc.close()
        html, csrf = page(client)
        assert 'id="setup-token"' in html
        # 捕获日志但不输出一次性验证码。
        logs = docker("logs", name)
        code = re.search(r"首次初始化验证码[^：]*：([^\s]+)", logs.stdout + logs.stderr).group(1)
        post(client, "/api/setup", csrf, {"password":password,"setup_token":code})
        with client.open(base + "/", timeout=5) as response:
            html = response.read().decode()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        with client.open(base + "/api/overview", timeout=5) as response:
            assert json.load(response)["schedule"] == {"value":1,"unit":"minutes"}
        with client.open(base + "/api/push-config", timeout=5) as response:
            push = json.load(response)
            assert not push["has_token"] and not push["enabled"] and not push["url"] and not push["group_id"]
        for asset in ("app.css", "app.js"):
            with client.open(base + "/static/" + asset, timeout=5) as response:
                assert response.status == 200 and response.read()
        post(client, "/api/schedule", csrf, {"value":2,"unit":"hours"})
        docker("restart", "--time", "120", name)
        wait_ready()
        client = browser()
        html, csrf = page(client)
        assert 'id="setup-token"' not in html
        post(client, "/api/login", csrf, {"password":password})
        with client.open(base + "/api/overview", timeout=5) as response:
            assert json.load(response)["schedule"] == {"value":2,"unit":"hours"}
        print("Container smoke test passed: initialization, login, assets, clean defaults and persisted settings.")
    finally:
        docker("rm", "--force", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "supply-monitor:smoke")
