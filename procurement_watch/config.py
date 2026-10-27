from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from .models import interval_seconds
from .collectors import COLLECTORS


def load_config(path: str) -> dict:
    file = Path(path).resolve()
    with file.open("rb") as stream:
        cfg = tomllib.load(stream)
    cfg["database"] = str(file.parent / cfg.get("database", "data/watch.db"))
    schedule = {"value": 1, "unit": "minutes", **cfg.get("schedule", {})}
    interval_seconds(schedule["value"], schedule["unit"])
    cfg["schedule"] = schedule
    ids = set()
    sources = cfg.get("sources", [])
    if not sources:
        raise ValueError("至少配置一个 sources")
    for source in sources:
        sid = source.get("id", "")
        if not sid or sid in ids:
            raise ValueError("source.id 必须非空且唯一")
        ids.add(sid)
        if source.get("type") not in COLLECTORS:
            raise ValueError(f"未注册的采集器类型：{source.get('type')}")
        validate_url(source.get("url", ""))
        if source.get("date_mode", "fixed") not in ("fixed", "rolling"):
            raise ValueError("date_mode 必须为 fixed 或 rolling")
        positive_int(source.get("lookback_days", 90), "lookback_days")
    keywords = cfg.get("keywords", [])
    if not isinstance(keywords, list) or any(not isinstance(k, str) or not k.strip() for k in keywords):
        raise ValueError("keywords 必须为非空字符串数组")
    http = cfg.get("http", {})
    positive_int(http.get("attempts", 3), "attempts")
    positive_number(http.get("timeout_seconds", 30), "timeout_seconds")
    notification = cfg.get("notification", {})
    for key, default in (("max_items", 5), ("max_chars", 3500)):
        positive_int(notification.get(key, default), key)
    if notification.get("max_chars", 3500) < 512:
        raise ValueError("max_chars 至少为 512")
    for key, default in (("timeout_seconds", 15), ("retry_seconds", 60)):
        positive_number(notification.get(key, default), key)
    if notification.get("success_mode", "onebot") not in ("onebot", "http"):
        raise ValueError("success_mode 必须为 onebot 或 http")
    if notification.get("enabled", False):
        validate_url(notification.get("url", ""))
        if not str(notification.get("group_id", "")).strip():
            raise ValueError("启用通知时必须配置 group_id")
    return cfg


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须为正整数")


def positive_number(value, name, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{name} 数值无效")


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
        raise ValueError("URL 必须为不含凭据的 HTTP/HTTPS 地址")
