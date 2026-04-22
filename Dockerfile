FROM mcr.microsoft.com/playwright/python:v1.58.0-noble AS builder

WORKDIR /app

USER root
RUN python -m pip install --no-cache-dir uv
RUN mkdir -p /app && chown -R pwuser:pwuser /app

USER pwuser
COPY --chown=pwuser:pwuser pyproject.toml uv.lock README.md /app/
COPY --chown=pwuser:pwuser src /app/src
RUN uv sync --frozen --no-dev

FROM mcr.microsoft.com/playwright/python:v1.58.0-noble AS runtime

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSER_SOURCE=bundled \
    CRAWLY_HOST=0.0.0.0 \
    CRAWLY_PORT=8000

COPY --from=builder --chown=pwuser:pwuser /app /app

USER pwuser

EXPOSE 8000

CMD ["crawly-mcp", "--transport", "streamable-http"]
