FROM docker.io/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY diagnostics.py downloader.py forge.py imagefiles.py maintenance.py ownership.py set_catalog.py sync.py sync_list.py upstream.py ./
COPY web/index.html web/app.css web/app.js ./web/
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8765/tcp
ENTRYPOINT ["python", "/app/downloader.py", "--bind", "0.0.0.0", "--data", "/data"]
