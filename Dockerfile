FROM docker.io/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY downloader.py forge.py upstream.py ./
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8765/tcp
ENTRYPOINT ["python", "/app/downloader.py", "--bind", "0.0.0.0", "--data", "/data"]
