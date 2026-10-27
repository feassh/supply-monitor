"""容器入口：固定端口和内置默认配置，所有业务设置由 Web 持久化管理。"""
import logging
import os
from pathlib import Path
import stat

APP_UID = 10001
APP_GID = 10001
DATA_DIRECTORY = Path("/app/data")
RUNTIME_FILES = ("watch.db", "watch.db-wal", "watch.db-shm", "watch.db-journal",
                 "watch.db.worker.lock", "watch.db.push.key")


def prepare_data_directory(path, uid=APP_UID, gid=APP_GID):
    """仅修复挂载根目录和已知运行文件，不递归修改其他宿主机文件。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(directory_fd, uid, gid)
        os.fchmod(directory_fd, stat.S_IMODE(os.fstat(directory_fd).st_mode) | 0o700)
        for name in RUNTIME_FILES:
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            try:
                mode = os.fstat(descriptor).st_mode
                if not stat.S_ISREG(mode):
                    raise RuntimeError(f"数据文件不是普通文件：{name}")
                os.fchown(descriptor, uid, gid)
                os.fchmod(descriptor, stat.S_IMODE(mode) | 0o600)
            finally:
                os.close(descriptor)
    finally:
        os.close(directory_fd)


def drop_privileges(uid=APP_UID, gid=APP_GID):
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.umask(0o077)


def initialize_runtime():
    if os.name != "posix":
        raise RuntimeError("容器入口仅用于 Linux；本地运行请使用 procurement_watch web")
    if os.geteuid() == 0:
        try:
            prepare_data_directory(DATA_DIRECTORY)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("无法初始化 /app/data 权限，请确认挂载可写，或在宿主机将目录及运行文件归属设置为 10001:10001") from exc
        drop_privileges()
    elif not os.access(DATA_DIRECTORY, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError("当前用户无法写入 /app/data，请在宿主机修复挂载目录权限")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    initialize_runtime()
    # 数据目录准备完成并降权之后，才加载和启动应用、SQLite、采集线程。
    from .config import load_config
    from .web import serve
    serve(load_config("/app/config.toml"), host="0.0.0.0", port=8000,
          production=True, initial_setup=True)


if __name__ == "__main__":
    main()
