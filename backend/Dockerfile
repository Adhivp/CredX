FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN python -m pip install --only-binary=:all: --require-hashes -r requirements.txt \
    && groupadd --gid 10001 credx \
    && useradd --uid 10001 --gid credx --no-create-home credx \
    && mkdir -p /app/data \
    && chown credx:credx /app/data \
    && chmod 700 /app/data

COPY backend/app ./app
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
# EWS imports these existing shared backend rules from the repository parent.
COPY core/__init__.py /core/__init__.py
COPY core/rules /core/rules

USER credx
EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=4)" || exit 1

# Shell evaluates &&; exec makes Uvicorn PID 1 for SIGTERM/graceful shutdown.
# No default JWT key or database is baked into this image.
CMD ["sh", "-c", "umask 077; python -m alembic upgrade head && exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 9000"]
