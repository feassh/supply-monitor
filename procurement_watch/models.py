from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntervalUnit(str, Enum):
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"


def interval_seconds(value: int, unit: str) -> int:
    factors = {u.value: f for u, f in zip(IntervalUnit, (60, 3600, 86400))}
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("采集频率必须是正整数")
    if unit not in factors:
        raise ValueError("频率单位必须为 minutes、hours 或 days")
    return value * factors[unit]


@dataclass(frozen=True)
class Article:
    source_id: str
    url: str
    title: str
    published_at: str
    buyer: str = ""
    agency: str = ""
    notice_type: str = ""
    region: str = ""
    category: str = ""
    summary: str = ""


@dataclass(frozen=True)
class ParsedPage:
    articles: list[Article]
    total_pages: int


def next_time(seconds: int) -> str:
    return (utcnow() + timedelta(seconds=seconds)).isoformat()
