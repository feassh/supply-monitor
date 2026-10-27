FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid monitor --no-create-home --home-dir /app --shell /usr/sbin/nologin monitor \
    && mkdir /app/data \
    && chown monitor:monitor /app/data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY procurement_watch/ ./procurement_watch/
COPY config.example.toml ./config.toml
RUN pip install --no-cache-dir --no-deps .

USER monitor
EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()"]

CMD ["python", "-m", "procurement_watch.container"]
