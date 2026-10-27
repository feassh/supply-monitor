from datetime import datetime, timedelta, timezone
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import Collector
from ..http import FetchError
from ..models import Article, ParsedPage

def clean(value: str) -> str:
    return " ".join(value.split())


class CcgpCollector(Collector):
    def __init__(self, source, client, http_config):
        self.source = source
        self.client = client

    def page_url(self) -> str:
        parts = urlsplit(self.source["url"])
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        params["page_index"] = "1"
        if self.source.get("date_mode", "fixed") == "rolling":
            today = datetime.now(timezone(timedelta(hours=8))).date()
            params.update(start_time=(today - timedelta(days=self.source.get("lookback_days", 90))).strftime("%Y:%m:%d"),
                          end_time=today.strftime("%Y:%m:%d"), timeType="4")
        # parse_qsl 保留 zoneId 中由 + 解码得到的空格，重新编码为合法查询参数。
        return urlunsplit(parts._replace(query=urlencode(params)))

    def collect(self) -> list[Article]:
        # 固定读取第一页，不跟随 HTML 中的翻页链接。
        url = self.page_url()
        parsed = self.parse(self.client.get(url), url)
        return list({item.url: item for item in parsed.articles}.values())

    def parse(self, html: str, page_url: str) -> ParsedPage:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        if any(marker in text for marker in ("您的访问过于频繁", "频繁访问!", "访问验证", "请输入验证码")):
            raise FetchError("政府采购网站返回访问限制/验证页面，请稍后重试")
        container = soup.select_one("ul.vT-srch-result-list-bid")
        if container is None:
            if re.search(r"共找到\s*0\s*条", text):
                return ParsedPage([], 1)
            raise FetchError("未找到搜索结果列表，页面结构可能变化或被拦截")
        items = []
        for li in container.find_all("li", recursive=False):
            anchor = li.find("a", href=True)
            span = li.find("span")
            if anchor is None or span is None:
                raise FetchError("公告列表项缺少标题、链接或发布时间，整轮采集取消")
            title = clean(anchor.get_text(" ", strip=True))
            url = urljoin(page_url, anchor["href"])
            if not title or urlsplit(url).scheme not in ("http", "https"):
                raise FetchError("公告标题或链接无效")
            metadata = clean(span.get_text(" ", strip=True))
            date = re.match(r"\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}", metadata)
            if not date:
                raise FetchError(f"公告缺少可识别的发布时间：{title}")
            published = datetime.strptime(date.group(), "%Y.%m.%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8))).isoformat()
            # br 将采购人/代理机构与公告类别、地区、品目分开。
            after_br = []
            header = metadata
            br = span.find("br")
            if br:
                after_br = [clean(v) for v in " ".join(
                    node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
                    for node in br.next_siblings).split("|")]
                header = clean(" ".join(
                    node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
                    for node in reversed(list(br.previous_siblings))))
            buyer = re.search(r"采购人[：:]\s*([^|]*)", header)
            agency = re.search(r"代理机构[：:]\s*([^|]*)", header)
            p = li.find("p")
            items.append(Article(
                source_id=self.source["id"], url=url, title=title, published_at=published,
                buyer=buyer.group(1).strip() if buyer else "",
                agency=agency.group(1).strip() if agency else "",
                notice_type=after_br[0] if after_br else "",
                region=after_br[1] if len(after_br) > 1 else "",
                category=after_br[2] if len(after_br) > 2 else "",
                summary=clean(p.get_text(" ", strip=True)) if p else "",
            ))
        if not items and not re.search(r"共找到\s*0\s*条", text):
            raise FetchError("结果列表异常为空")
        page_sizes = [int(v) for v in re.findall(r"\bsize\s*:\s*(\d+)", html)]
        page_links = [int(v) for v in re.findall(r"gopage\((\d+)\)", html)]
        return ParsedPage(items, max([1, *page_sizes, *page_links]))
