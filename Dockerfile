# ChipForge SN108 validator / miner image.
#   docker compose --profile validator up -d     (see Makefile / README "Running with Docker")

FROM python:3.12-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt requirements-lock.txt /tmp/
# requirements-lock.txt pins the exact set the test-suite ran against
RUN pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/requirements-lock.txt

FROM python:3.12-slim
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} chipforge && useradd -m -u ${UID} -g ${GID} -s /usr/sbin/nologin chipforge \
    && mkdir -p /data /wallets && chown chipforge:chipforge /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root chipforge ./chipforge
COPY --chown=root:root neurons ./neurons
COPY --chown=root:root python_scripts ./python_scripts
COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/chipforge-entrypoint
RUN chmod 0755 /usr/local/bin/chipforge-entrypoint

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app:/app/neurons \
    PYTHONUNBUFFERED=1 \
    CHIPFORGE_DATA_DIR=/data \
    MINER_CHALLENGE_DIR=/data/downloaded_active_challenge \
    WALLET_PATH=/wallets

USER chipforge
VOLUME ["/data"]
ENTRYPOINT ["chipforge-entrypoint"]
CMD ["validator"]
