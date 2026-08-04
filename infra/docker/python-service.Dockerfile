# Python 服务镜像同时承载 gateway 与 control-api；启动命令由 compose service 注入。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先安装运行时依赖，减少后续源码变更导致的镜像层失效。
COPY infra/docker/requirements.runtime.txt /tmp/metahub-requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/metahub-requirements.txt

# 只复制运行所需源码、迁移和种子文件，避免把本地缓存或 secret 打包进镜像。
COPY shared-sdk/python /app/shared-sdk/python
COPY gateway-data-plane/src /app/gateway-data-plane/src
COPY control-plane-api/src /app/control-plane-api/src
COPY infra/migrations /app/infra/migrations
COPY infra/seeds /app/infra/seeds

RUN mkdir -p /app/.data/compose /app/.logs

EXPOSE 8000 8001

CMD ["python", "-m", "metahub_gateway.main"]
