# Immutable base and a hash-locked dependency closure define the trial runtime.
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/shared-sdk/python:/app/gateway-data-plane/src:/app/control-plane-api/src
WORKDIR /app
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261013T000000Z bookworm main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261013T000000Z bookworm-security main' > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends libxcb1 libx11-6 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY infra/production/requirements.lock /tmp/requirements.lock
RUN python -m pip install --require-hashes -r /tmp/requirements.lock
RUN python -c "import cv2, onnxruntime; from rapidocr_onnxruntime import RapidOCR"
# A readiness probe must not start another Python interpreter and import the
# server every 15 seconds. Keep this layer after the locked Python dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY shared-sdk/python/metahub_shared /app/shared-sdk/python/metahub_shared
COPY gateway-data-plane/src /app/gateway-data-plane/src
COPY control-plane-api/src /app/control-plane-api/src
# Controlled SDK examples back the metadata and download endpoints. Production
# builds use the source allowlist; .env.example is a template, never host .env.
COPY examples/python /app/examples/python
COPY examples/js /app/examples/js
COPY infra/migrations /app/infra/migrations
COPY infra/seeds /app/infra/seeds
COPY infra/production/service.py /app/service.py
COPY scripts/deploy/sqlite-to-postgres.py /app/tools/sqlite-to-postgres.py
COPY scripts/deploy/bootstrap-owner.py /app/tools/bootstrap-owner.py
COPY scripts/deploy/database-fingerprint.py /app/tools/database-fingerprint.py
# Older installed updaters can stage source directories under umask 077.
# Image source is public release content; normalize traversal before switching
# to the runtime user, without changing deployment secret or volume permissions.
RUN find /app -type d -exec chmod 755 {} +
# Read-only workers cannot populate bytecode caches on first import. Hash-based
# caches at fixed image paths avoid runtime compilation and timestamp drift.
RUN python -m compileall -q --invalidation-mode checked-hash -x '(^|/)(test|tests)/' /app /usr/local/lib/python3.12
RUN groupadd --gid 10001 metahub && useradd --uid 10001 --gid 10001 --no-create-home metahub \
    && mkdir -p /app/.data /app/.logs && chown -R 10001:10001 /app/.data /app/.logs
# Release metadata belongs after dependency layers. A version/commit change must
# not force the same locked runtime dependencies to be installed again.
ARG RELEASE_VERSION=2.0.0-trial.45
ARG BUILD_COMMIT=working-tree
ENV METAHUB_VERSION=${RELEASE_VERSION} METAHUB_BUILD_COMMIT=${BUILD_COMMIT}
USER 10001:10001
LABEL org.opencontainers.image.version=${RELEASE_VERSION} org.opencontainers.image.title="MetaHub trial backend"
ENTRYPOINT ["python", "/app/service.py"]
CMD ["control"]
