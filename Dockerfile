FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    PORT=10000

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN useradd -m -u 1000 user
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY examples ./examples
COPY frontend ./frontend
COPY deploy ./deploy

RUN uv sync --frozen --no-dev \
    && chmod +x /app/deploy/docker/entrypoint.sh \
    && chown -R user:user /app

USER user
EXPOSE 10000

ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
