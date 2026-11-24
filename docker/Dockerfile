FROM docker.io/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY LICENSE ./
COPY scryfall_downloader/ ./scryfall_downloader/
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8765/tcp
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD ["python", "-m", "scryfall_downloader.healthcheck"]
ENTRYPOINT ["python", "-m", "scryfall_downloader", "--bind", "0.0.0.0", "--data", "/data"]
