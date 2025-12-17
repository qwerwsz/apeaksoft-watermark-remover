# Apeaksoft Watermark Remover Proxy

FastAPI service that forwards image erase requests to Apeaksoft’s watermark remover endpoints, handles signing and headers, and persists call history/results locally.

## Features
- Multipart `/api/erase` endpoint: validates image/mask, computes sign, forwards to `removeWM/upload`, polls initial WM status, and returns a token.
- `/api/erase/status` endpoint: forwards token to `removeWM/status`, returns upstream JSON, and records the final result URL in SQLite.
- Benefit limit checks (size/resolution) against the upstream benefit status API.
- Request/response logging to `logs/app.log` and persistence of request metadata and image binaries to `data/api_calls.db`.

## Quick Start

### Option 1: Docker (Recommended)

Pull and run the pre-built image:

```bash
# Pull the latest image
docker pull ghcr.io/qwerwsz/apeaksoft-watermark-remover:latest

# Run the container
docker run -d \
  --name watermark-remover \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/qwerwsz/apeaksoft-watermark-remover:latest

# Or use docker-compose
cat > docker-compose.yml <<EOF
version: '3.8'
services:
  watermark-remover:
    image: ghcr.io/qwerwsz/apeaksoft-watermark-remover:latest
    container_name: watermark-remover
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    restart: unless-stopped
EOF

docker-compose up -d
```

Access the API at: http://localhost:8000

Swagger UI: http://localhost:8000/docs

### Option 2: Build Docker Image Locally

```bash
# Build the image
docker build -t watermark-remover .

# Run the container
docker run -d -p 8000:8000 -v $(pwd)/data:/app/data -v $(pwd)/logs:/app/logs watermark-remover
```

### Option 3: Run with Python

Prerequisites:
- Python 3.11+ recommended
- `pip` available in PATH

Setup:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Access the API at: http://127.0.0.1:8000

Swagger UI: http://127.0.0.1:8000/docs

## API

### 1) 擦除图片 `/api/erase` (POST, multipart/form-data)
Fields:
- `img` (file, required): original image. Allowed types: `image/jpeg`, `image/png`, `image/jpg`, `image/webp`. Max 50MB (plus upstream benefit limits).
- `mask` (file, required): mask image matching the area to remove.

Response (200):
```json
{
  "token": "dc266d46-1491-4163-8830-2f0622e44a05",
  "message": "已提交擦除请求，正在处理中.."
}
```

Notes:
- The service sends a trial request, generates `e_id`, computes `sign` for the image, and uploads to `https://ai-api.apeaksoft.com/v6/removeWM/upload`.
- It immediately calls `https://ai-api.apeaksoft.com/v6/removeWM/WM` once to get initial status.
- Benefit limits are fetched from `https://account.api.apeaksoft.com/v9/benefit/status`; if limits are exceeded, a 400 error is returned.

### 2) 查询状态 `/api/erase/status` (POST, application/json)
Body:
```json
{ "token": "dc266d46-1491-4163-8830-2f0622e44a05" }
```

Behavior:
- Forwards to `https://ai-api.apeaksoft.com/v6/removeWM/status`.
- Returns the upstream JSON as-is.
- If the response contains a URL (top-level `url`, `data.url`, or `result.url`), it updates `result_url` in SQLite for the given token.

Example upstream response:
```json
{
  "status": "200",
  "task_id": "2754920",
  "token": "dc266d46-1491-4163-8830-2f0622e44a05",
  "url": "https://www.istorage-cloud.com/watermark/uAfasmPasf/login/2025-12-17/0a97c42057f24b6caeaa0b6f2c2e41da/42042015.jpeg"
}
```

## Data & Logs
- DB: `data/api_calls.db` (auto-created on startup). Tables/fields defined in `database.py`. Images are stored as BLOBs.
- Logs: `logs/app.log` (auto-created). Log level defaults to DEBUG for app, INFO for httpx.

## Project Structure
- `main.py` — FastAPI app, routes, validation.
- `core.py` — Upstream HTTP helpers, signing, client headers.
- `database.py` — SQLite helpers (aiosqlite) for call history/results.
- `logs/`, `data/` — created automatically.

## Docker Image Versions

The Docker images are automatically built and published via GitHub Actions:

- `latest` - Latest stable version from master branch
- `v1.0.0` - Specific release version
- `1.0`, `1` - Major/minor version tags
- `master-sha-xxxxxx` - Specific commit SHA

## Development Tips
- Run with `--reload` during local dev.
- Adjust size limits or headers in `main.py` / `core.py` as needed.
- Network calls require outbound access; failures will appear in `logs/app.log`.

## CI/CD

This project uses GitHub Actions for automated Docker image builds:
- Triggers on push to master/main branches
- Triggers on version tags (v*)
- Multi-platform builds (linux/amd64, linux/arm64)
- Automatic publishing to GitHub Container Registry (ghcr.io)
