import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .push_config import effective_config, public_config

log = logging.getLogger(__name__)


def format_item(row, index: int) -> str:
    keywords = "、".join(json.loads(row["matched_keywords"]))
    return (f"{index}. {row['title']}\n"
            f"发布时间：{row['published_at'].replace('T', ' ')}\n"
            f"采购人：{row['buyer'] or '未提供'}\n"
            f"代理机构：{row['agency'] or '未提供'}\n"
            f"类型：{row['notice_type']} | 地区：{row['region']}\n"
            f"匹配关键词：{keywords}\n"
            f"{row['url']}")


def message_batches(rows, max_items: int, max_chars: int):
    header = "采购公告更新\n\n"
    batch, text = [], header
    for row in rows:
        item = format_item(row, len(batch) + 1)
        if batch and (len(batch) >= max_items or len(text) + len(item) + 2 > max_chars):
            yield batch, text.rstrip()
            batch, text = [], header
            item = format_item(row, 1)
        if len(header) + len(item) > max_chars:
            # 不能截断 URL 或默默丢弃字段；让配置错误可见并保留队列。
            raise ValueError("单条公告超过 max_chars，请提高消息长度上限")
        batch.append(row["notification_id"])
        text += item + "\n\n"
    if batch:
        yield batch, text.rstrip()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API 重定向视为失败，避免将鉴权头转发到其他目标。
        return None


class QQWebhook:
    """默认采用 OneBot send_group_msg 契约，自建机器人在此适配请求/回执。"""
    def __init__(self, config: dict):
        self.config = config

    def send(self, text: str):
        payload = {"message_type": "group", "group_id": self.config["group_id"], "message": text, "auto_escape": True}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        token = self.config.get("token", os.environ.get(self.config.get("token_env", "QQ_BOT_TOKEN"), ""))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.config["url"], data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.config.get("timeout_seconds", 15)) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"通知 HTTP 状态异常：{response.status}")
                body = response.read(1_000_000)
                if self.config.get("success_mode", "onebot") == "onebot":
                    try:
                        result = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise RuntimeError("机器人返回的内容不是有效 JSON") from None
                    if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode") != 0:
                        raise RuntimeError("机器人未返回 OneBot 成功回执")
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError(f"通知 HTTP 请求失败：{code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            # 不输出 URL、请求体、Token，避免凭据进入日志。
            raise RuntimeError(f"通知网络请求失败：{type(exc).__name__}") from exc


def dispatch(store, config: dict, sender=None, preview=False, stop_event=None) -> int:
    if not preview and not public_config(store, config)["enabled"]:
        return 0
    if not preview:
        try:
            config = effective_config(store, config)
        except ValueError as exc:
            log.error("推送配置不可用，保留待发送通知：%s", exc)
            return 0
    rows = store.pending(limit=10000, include_delayed=preview)
    sender = sender or QQWebhook(config)
    sent = 0
    try:
        for ids, text in message_batches(rows, config.get("max_items", 5), config.get("max_chars", 3500)):
            if stop_event is not None and stop_event.is_set():
                break
            if preview:
                print(text + "\n")
                continue
            try:
                sender.send(text)
            except Exception as exc:
                store.delivery_result(ids, str(exc), config.get("retry_seconds", 60))
                log.error("通知发送失败，已保留 %s 条待重试：%s", len(ids), exc)
                # 机器人不可用时停止本轮发送，避免对同一接口连续请求。
                break
            store.delivery_result(ids)
            sent += len(ids)
    except ValueError as exc:
        log.error("通知排版失败，保留待发送记录：%s", exc)
    return sent
