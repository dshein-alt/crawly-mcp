FROM python:3.13-bookworm AS builder

WORKDIR /app

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md LICENSE /app/
COPY src /app/src
RUN uv sync --frozen --no-dev

FROM python:3.13-bookworm AS runtime

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSER_SOURCE=bundled \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CRAWLY_HOST=0.0.0.0 \
    CRAWLY_PORT=8000 \
    CRAWLY_PROFILE_DIR=/data/profiles \
    CRAWLY_PROFILE_CLEANUP_ON_START=true \
    HOME=/home/app

COPY --from=builder /app /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /data/profiles /ms-playwright \
    && /app/.venv/bin/patchright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R app:app /app /data/profiles /ms-playwright

USER app

EXPOSE 8000
CMD ["crawly-mcp", "--transport", "streamable-http"]
