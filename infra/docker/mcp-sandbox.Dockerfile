FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/shared-sdk/python:/app/mcp-sandbox-worker/src

WORKDIR /app

COPY infra/docker/requirements.mcp-sandbox.txt /tmp/metahub-requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/metahub-requirements.txt \
    && addgroup --system --gid 65532 metahub \
    && adduser --system --uid 65532 --ingroup metahub --no-create-home metahub

COPY shared-sdk/python /app/shared-sdk/python
COPY mcp-sandbox-worker/src /app/mcp-sandbox-worker/src

USER 65532:65532

EXPOSE 8091

CMD ["python", "-m", "metahub_mcp_sandbox.main"]
