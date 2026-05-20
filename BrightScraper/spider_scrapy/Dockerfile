FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && useradd --create-home --shell /bin/bash appuser

COPY . .

RUN mkdir -p /app/data /app/insta_dump \
    && chown -R appuser:appuser /app /ms-playwright

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8000\")}/health', timeout=4)" || exit 1

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT}"]
