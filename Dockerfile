FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY catalog.json sessions.json ./
# Audio files are the product. They are NOT in git (see .gitignore).
# Copy them into ./audio/ before building, or mount them at deploy time.
# Tracks without a file return 503 from /stream — never a synthetic tone.
COPY audio/ ./audio/

RUN mkdir -p /app/data
EXPOSE 8000

# Port 8000 explicitly: fly.toml's http_service internal_port is 8000, so the
# app must listen there regardless of any $PORT the platform sets.
# scripts/build_image.py (hand-built image, used when Fly's remote builders
# are unreachable) uses this same command.
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
