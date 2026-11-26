FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
VOLUME /data
EXPOSE 8080
# One worker on purpose: SQLite + a single writer keeps the ledger simple and safe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
