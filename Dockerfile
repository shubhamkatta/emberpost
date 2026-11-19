FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY warmup ./warmup

# Unprivileged user; /data holds warmup.db (set `database: /data/warmup.db` in config.yaml)
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin emberpost \
 && mkdir /data && chown emberpost /data
USER emberpost
VOLUME /data

# Unhealthy if the loop stopped ticking or no tick has succeeded for hours
HEALTHCHECK --interval=5m --timeout=30s --start-period=3m --retries=2 \
  CMD ["python", "-m", "warmup", "health", "-c", "/config/config.yaml"]

# Use llm.provider: api here. The `claude` CLI login is not available inside the container.
CMD ["python", "-m", "warmup", "run", "-c", "/config/config.yaml"]
