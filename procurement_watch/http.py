import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FetchError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, config: dict):
        self.timeout = config.get("timeout_seconds", 30)
        self.attempts = config.get("attempts", 3)
        self.user_agent = config.get("user_agent", "ProcurementWatch/0.1")

    def get(self, url: str) -> str:
        for attempt in range(self.attempts):
            try:
                request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "text/html"})
                with urlopen(request, timeout=self.timeout) as response:
                    data = response.read(5_000_001)
                    if len(data) > 5_000_000:
                        raise FetchError("响应 HTML 超过 5MB")
                    encoding = response.headers.get_content_charset() or "utf-8"
                    return data.decode(encoding, errors="replace")
            except HTTPError as exc:
                if exc.code == 429 or exc.code in (401, 403):
                    raise FetchError(f"站点限制访问，HTTP {exc.code}，请降低频率后重试") from exc
                if exc.code < 500 or attempt + 1 == self.attempts:
                    raise FetchError(f"采集请求失败，HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt + 1 == self.attempts:
                    raise FetchError(f"采集网络请求失败：{type(exc).__name__}") from exc
            time.sleep(2 ** attempt)
        raise FetchError("采集失败")
