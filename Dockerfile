FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY google_channel ./google_channel
COPY dashboard ./dashboard-source
RUN python -m google_channel.dashboard --source /app/dashboard-source --output /app/dashboard && \
    rm -rf /app/dashboard-source && pip install --no-cache-dir . && pip check && \
    useradd --uid 10001 --system --home /nonexistent google-channel && \
    mkdir -p /data/google && chown 10001:10001 /data/google
ENV GOOGLE_DASHBOARD_DIR=/app/dashboard \
    GOOGLE_STATE_DB=/data/google/state.sqlite3 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000 8001 8002
CMD ["python", "-m", "google_channel.server"]
