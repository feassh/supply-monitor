from .ccgp import CcgpCollector


# 每种站点通过同一接口提供自己的查询和解析策略。
COLLECTORS = {"ccgp": CcgpCollector}


def create_collector(source: dict, client, http_config: dict):
    return COLLECTORS[source["type"]](source, client, http_config)
