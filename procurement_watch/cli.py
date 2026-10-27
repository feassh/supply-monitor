import argparse
import json
import logging
from pathlib import Path
import signal
import sqlite3
import sys
import threading

from .collectors import create_collector
from .config import load_config
from .notifications import dispatch
from .service import WatchService
from .storage import Store, worker_lock


def parser():
    p = argparse.ArgumentParser(description="采购公告采集、关键词管理与 QQ 通知后端")
    p.add_argument("--config", default="config.toml", help="TOML 配置文件")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("init", "status", "run"):
        sub.add_parser(command)
    once = sub.add_parser("once", help="立即采集并处理通知")
    once.add_argument("--source")
    listing = sub.add_parser("list", help="查看已存公告")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--keyword")
    keyword = sub.add_parser("keyword")
    keyword.add_argument("action", choices=("list", "add", "remove"))
    keyword.add_argument("value", nargs="?")
    schedule = sub.add_parser("schedule")
    schedule.add_argument("value", type=int)
    schedule.add_argument("unit", choices=("minutes", "hours", "days"))
    fixture = sub.add_parser("import-html", help="导入已保存 HTML，供离线验证；会更新该来源基线")
    fixture.add_argument("file")
    fixture.add_argument("--source", required=True)
    notify = sub.add_parser("notify", help="发送到期的待通知公告")
    notify.add_argument("--preview", action="store_true", help="仅预览，不发送、不标记成功")
    web = sub.add_parser("web", help="启动带登录保护的管理界面和采集工作进程")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--secure-cookie", action="store_true", help="HTTPS 部署时启用 Secure Cookie")
    web.add_argument("--trust-proxy", action="store_true", help="信任单层反向代理传递的协议，仅用于可信代理部署")
    sub.add_parser("web-password", help="在终端设置或重置管理密码")
    return p


def dump(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def execute(args, cfg, store):
    service = WatchService(store, cfg)
    sources = {s["id"]: s for s in cfg["sources"]}
    if getattr(args, "source", None) and args.source not in sources:
        raise ValueError(f"未知来源：{args.source}")
    if args.command == "web":
        from .web import serve
        serve(cfg, args.host, args.port, args.secure_cookie, args.trust_proxy)
    elif args.command == "web-password":
        from getpass import getpass
        from .web import set_password
        password = getpass("请输入管理密码（至少 10 位）：")
        if password != getpass("请再次输入管理密码："):
            raise ValueError("两次输入的密码不一致")
        set_password(store, password)
        print("管理密码已保存，已有登录会话已失效。")
    elif args.command == "init":
        print(f"数据库已初始化：{cfg['database']}")
    elif args.command == "status":
        dump(store.status())
    elif args.command == "list":
        if args.limit < 1:
            raise ValueError("limit 必须大于 0")
        dump([dict(r) for r in store.records(args.limit, args.keyword)])
    elif args.command == "keyword":
        if args.action != "list":
            if args.value is None:
                raise ValueError("请提供关键词")
            store.change_keyword(args.value, remove=args.action == "remove")
        dump(store.keywords())
    elif args.command == "schedule":
        store.set_schedule(args.value, args.unit)
        dump(store.setting("schedule"))
    elif args.command == "import-html":
        source = sources[args.source]
        collector = create_collector(source, service.client, cfg.get("http", {}))
        parsed = collector.parse(Path(args.file).read_text(encoding="utf-8-sig"), source["url"])
        dump(service.ingest(args.source, parsed.articles))
    elif args.command == "notify":
        sent = dispatch(store, cfg.get("notification", {}), preview=args.preview)
        if not args.preview:
            dump({"sent": sent, "notifications": store.status()["notifications"]})
    elif args.command == "once":
        success = service.collect(source_id=args.source)
        service.notify()
        return 0 if success else 1
    elif args.command == "run":
        stop = threading.Event()
        def shutdown(signum, frame):
            stop.set()
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        logging.info("采集工作进程已启动，Ctrl+C 停止")
        while not stop.is_set():
            service.collect(due_only=True)
            service.notify()
            stop.wait(5)
    return 0


def main():
    # Windows 控制台、管道和重定向均以 UTF-8 输出中文。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parser().parse_args()
    store = None
    try:
        cfg = load_config(args.config)
        store = Store(cfg["database"])
        store.bootstrap(cfg)
        if args.command in ("run", "once", "import-html", "notify"):
            with worker_lock(cfg["database"]):
                result = execute(args, cfg, store)
        else:
            result = execute(args, cfg, store)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        logging.error("%s", exc)
        result = 1
    finally:
        if store:
            store.close()
    raise SystemExit(result)
