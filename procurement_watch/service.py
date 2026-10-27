import logging

from .collectors import create_collector
from .http import HttpClient
from .models import interval_seconds
from .notifications import dispatch

log = logging.getLogger(__name__)


class WatchService:
    def __init__(self, store, config, client=None):
        self.store = store
        self.config = config
        self.client = client or HttpClient(config.get("http", {}))

    def interval(self):
        schedule = self.store.setting("schedule")
        return interval_seconds(schedule["value"], schedule["unit"])

    def ingest(self, source_id, articles):
        return self.store.ingest(source_id, articles, self.interval(), self.config.get("notify_on_first_run", False))

    def collect(self, due_only=False, source_id=None) -> bool:
        if due_only and self.store.setting("collection_paused"):
            return True
        success = True
        for source in self.config["sources"]:
            if not source.get("enabled", True) or (source_id and source["id"] != source_id):
                continue
            if due_only and not self.store.due(source["id"]):
                continue
            try:
                collector = create_collector(source, self.client, self.config.get("http", {}))
                result = self.ingest(source["id"], collector.collect())
                log.info("%s 采集完成：%s", source["id"], result)
            except Exception as exc:
                success = False
                # 限流后不立即重试整轮；失败不会修改已存公告、首次基线或成功时间。
                self.store.record_failure(source["id"], str(exc), max(300, self.interval()))
                log.error("%s 采集失败：%s", source["id"], exc)
        return success

    def notify(self, stop_event=None):
        return dispatch(self.store, self.config.get("notification", {}), stop_event=stop_event)
