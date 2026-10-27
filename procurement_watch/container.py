"""容器入口：固定端口和内置默认配置，所有业务设置由 Web 持久化管理。"""
import logging

from .config import load_config
from .web import serve


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve(load_config("/app/config.toml"), host="0.0.0.0", port=8000,
          production=True, initial_setup=True)


if __name__ == "__main__":
    main()
