# Pinned by digest (multi-arch index of python:3.12.15-slim). Update the tag and digest together.
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY requirements.txt .
# requirements.txt is a hashed lockfile compiled from requirements.in (see CLAUDE.md)
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY app ./app
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
VOLUME /data
EXPOSE 8080
# One worker on purpose: SQLite + a single writer keeps the ledger simple and safe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
