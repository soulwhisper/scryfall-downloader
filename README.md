# Scryfall downloader for Forge

Download Magic: The Gathering set images for Forge through a Web UI or REST API.
Run from source with Python 3.11+; no pip packages are needed.

## Run

From the repository root:

```sh
python3 -m scryfall_downloader
```

Or build and run a container from the same directory:

```sh
mkdir -p data
docker build -f docker/Dockerfile -t scryfall-downloader:dev .
docker run --rm --name scryfall-downloader --stop-timeout 30 \
  --user "$(id -u):$(id -g)" -p 127.0.0.1:8765:8765 \
  -v "$PWD/data:/data" scryfall-downloader:dev
```

Open <http://127.0.0.1:8765/> for the Web UI; REST clients use `/api/v1`.
Set [`DOWNLOADER_PORT`](docs/operations.md#listening-port) to change the default `8765`.
The local server defaults to the repository's `data/` directory (`--data DIR`
overrides it); the container uses `/data`. Keep one downloader per data directory.
For LAN access, see [authentication and HTTPS](docs/operations.md#network-and-proxies).

- [REST API](docs/api.md): jobs, catalogue sync, saved sync list, and diagnostics.
- [Forge image library](docs/library.md): layout, art selection, repair, and upgrades.
- [Operations](docs/operations.md): first-start backfill, schedules, proxies, containers, and releases.
