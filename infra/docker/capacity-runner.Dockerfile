FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/control-plane-api/src

WORKDIR /app

COPY infra/docker/requirements.capacity-runner.txt /tmp/metahub-requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/metahub-requirements.txt \
    && addgroup --system --gid 65532 metahub \
    && adduser --system --uid 65532 --ingroup metahub --no-create-home metahub

COPY control-plane-api/src/metahub_capacity_runner /app/control-plane-api/src/metahub_capacity_runner

USER 65532:65532

EXPOSE 8012

CMD ["python", "-m", "metahub_capacity_runner.main"]
