from abc import ABC, abstractmethod

from ..models import Article, ParsedPage


class Collector(ABC):
    @abstractmethod
    def collect(self) -> list[Article]:
        """一次采集成功才交给业务层入库；失败时抛出异常。"""

    @abstractmethod
    def parse(self, html: str, page_url: str) -> ParsedPage:
        """解析列表页，不访问公告详情页。"""
