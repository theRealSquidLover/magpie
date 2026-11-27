# Dev container for StockApp. Same pinned base as the app image (see ../Dockerfile and CLAUDE.md).
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# Debian packages come from Debian's signed archive: git, poppler-utils (PDF tests), nodejs (tests/check_js.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git poppler-utils nodejs \
 && rm -rf /var/lib/apt/lists/*

# Python deps from the hashed lockfile, wheels only (no install-time builds).
COPY requirements.txt requirements-dev.txt /tmp/req/
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/req/requirements-dev.txt \
 && rm -rf /tmp/req

# Chromium for the e2e scripts, at the build that matches the pinned Playwright version.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium

# Unprivileged user for day-to-day work.
RUN useradd --create-home --shell /bin/bash dev
USER dev
