# 控制台镜像使用仓库内无依赖 Node 静态服务器，避免把管理员密码或 Provider Key 注入前端产物。
FROM node:22-alpine

WORKDIR /app/console-web

COPY console-web/package.json /app/console-web/package.json
COPY console-web/index.html /app/console-web/index.html
COPY console-web/scripts /app/console-web/scripts
COPY console-web/src /app/console-web/src

ENV METAHUB_CONSOLE_PORT=5173

EXPOSE 5173

CMD ["node", "scripts/dev-server.mjs"]
