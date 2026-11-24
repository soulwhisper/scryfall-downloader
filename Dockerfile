FROM docker.io/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY diagnostics.py downloader.py forge.py imagefiles.py maintenance.py ownership.py set_catalog.py sync.py sync_list.py upstream.py ./
COPY web/index.html web/app.css web/app.js ./web/
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8765/tcp
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "from http.client import HTTPConnection; c = HTTPConnection('127.0.0.1', 8765, timeout=3); c.request('GET', '/api/v1/health'); raise SystemExit(c.getresponse().status != 200)"]
ENTRYPOINT ["python", "/app/downloader.py", "--bind", "0.0.0.0", "--data", "/data"]
