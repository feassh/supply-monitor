"""Web、CLI、后台任务共用的推送配置及令牌加密存储。"""
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .config import validate_url

_key_guard = threading.Lock()


def _cipher(store, create=False):
    path = Path(store.path + ".push.key")
    with _key_guard:
        if not path.exists():
            if not create:
                raise ValueError("推送令牌密钥缺失，请恢复密钥或重新填写访问令牌")
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
        try:
            return Fernet(path.read_bytes())
        except (ValueError, OSError) as exc:
            raise ValueError("推送令牌密钥无法读取，请恢复密钥或重新配置令牌") from exc


def effective_config(store, defaults):
    result = dict(defaults)
    saved = store.setting("push_config")
    if saved:
        result.update({key: saved[key] for key in ("enabled", "url", "group_id")})
        mode = saved.get("token_mode", "environment")
        if mode == "stored":
            try:
                result["token"] = _cipher(store).decrypt(saved["token_ciphertext"].encode()).decode()
            except (InvalidToken, UnicodeError) as exc:
                raise ValueError("推送令牌无法解密，请重新填写访问令牌") from exc
        elif mode == "none":
            result["token"] = ""
    result.setdefault("token", os.environ.get(result.get("token_env", "QQ_BOT_TOKEN"), ""))
    return result


def public_config(store, defaults):
    # 读取表单无需解密，密钥丢失时仍能通过表单修复配置。
    saved = store.setting("push_config") or {}
    result = {key: saved.get(key, defaults.get(key, fallback)) for key, fallback in (
        ("enabled", False), ("url", ""), ("group_id", ""))}
    mode = saved.get("token_mode", "environment")
    result["has_token"] = bool(saved.get("token_ciphertext")) if mode == "stored" else (
        bool(os.environ.get(defaults.get("token_env", "QQ_BOT_TOKEN"), "")) if mode == "environment" else False)
    result["token_source"] = mode
    result["last_test"] = store.setting("push_last_test")
    result["pending_count"] = store.db.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0]
    return result


def save_config(store, defaults, data):
    enabled = data.get("enabled")
    url, group = data.get("url"), data.get("group_id")
    if not isinstance(enabled, bool):
        raise ValueError("推送开关无效")
    if not isinstance(url, str) or not isinstance(group, str):
        raise ValueError("请填写机器人接口 URL 和群组 ID")
    url, group = url.strip(), group.strip()
    if url:
        if len(url) > 2048:
            raise ValueError("机器人接口 URL 过长")
        validate_url(url)
        parts = urlsplit(url)
        if parts.fragment or not parts.hostname:
            raise ValueError("机器人接口 URL 格式无效")
        try:
            parts.port
        except ValueError:
            raise ValueError("机器人接口端口无效") from None
    if group and (not group.isascii() or not group.isdigit() or len(group) > 20):
        raise ValueError("群组 ID 应为不超过 20 位的数字")
    if enabled and (not url or not group):
        raise ValueError("启用推送前，请完整填写接口 URL 和群组 ID")
    token = data.get("token", "")
    if not isinstance(token, str) or len(token) > 4096 or "\n" in token or "\r" in token:
        raise ValueError("访问令牌格式无效")
    token = token.strip()
    clear = data.get("clear_token", False)
    if not isinstance(clear, bool) or (clear and token):
        raise ValueError("请勿同时填写和清除令牌")
    existing = store.setting("push_config") or {}
    saved = {"enabled": enabled, "url": url, "group_id": group,
             "token_mode": existing.get("token_mode", "environment")}
    if token:
        saved.update(token_mode="stored", token_ciphertext=_cipher(store, create=True).encrypt(token.encode()).decode())
    elif clear:
        saved["token_mode"] = "none"
    elif existing.get("token_ciphertext"):
        saved["token_ciphertext"] = existing["token_ciphertext"]
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO settings VALUES ('push_config', ?)", (json.dumps(saved),))
        if saved != existing:
            store.db.execute("DELETE FROM settings WHERE key='push_last_test'")
    return public_config(store, defaults)
