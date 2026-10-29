FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/control-plane-api/src:/app/shared-sdk/python:/app/gateway-data-plane/src

WORKDIR /app
COPY infra/docker/requirements.runtime.txt /tmp/metahub-requirements.txt
RUN python -m pip install -r /tmp/metahub-requirements.txt

COPY shared-sdk/python /app/shared-sdk/python
COPY gateway-data-plane/src /app/gateway-data-plane/src
COPY control-plane-api/src /app/control-plane-api/src
COPY infra/migrations /app/infra/migrations
COPY infra/seeds /app/infra/seeds
COPY infra/docker/prepare_assurance_guest.py /app/prepare_assurance_guest.py
RUN python /app/prepare_assurance_guest.py

USER 10001:10001
HEALTHCHECK NONE
ENTRYPOINT ["/usr/local/bin/python"]
CMD ["-m", "metahub_assurance_runner.guest"]
