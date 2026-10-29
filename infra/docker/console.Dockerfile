# 控制台构建只输出验证后的生产入口图，退休源码不会进入运行镜像。
FROM node:22-alpine AS build

WORKDIR /app/console-web

COPY console-web/package.json /app/console-web/package.json
COPY console-web/index.html /app/console-web/index.html
COPY console-web/identity.html /app/console-web/identity.html
COPY console-web/partner.html /app/console-web/partner.html
COPY console-web/platform.html /app/console-web/platform.html
COPY console-web/status.html /app/console-web/status.html
COPY console-web/production-surface-manifest.json /app/console-web/production-surface-manifest.json
COPY console-web/scripts /app/console-web/scripts
COPY console-web/src /app/console-web/src

RUN npm run build

# 运行镜像仅携带静态服务器和经过清单验证的 dist，不包含完整 console-web/src。
FROM node:22-alpine

WORKDIR /app

COPY --from=build /app/console-web/dist /app/console-web
COPY console-web/scripts/dev-server.mjs /app/dev-server.mjs

ENV METAHUB_CONSOLE_PORT=5173
ENV METAHUB_CONSOLE_ROOT=/app/console-web

EXPOSE 5173

CMD ["node", "dev-server.mjs"]
