FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
WORKDIR /build/console-web
COPY console-web/package.json console-web/production-surface-manifest.json ./
COPY console-web/*.html ./
COPY console-web/scripts ./scripts
COPY console-web/src ./src
COPY infra/production/console.Dockerfile /build/infra/production/console.Dockerfile
RUN npm run build

# This runtime contains only verified build output; no Node or development server.
FROM nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236
ARG RELEASE_VERSION=2.0.0-trial.45
COPY --from=build /build/console-web/dist /usr/share/nginx/html
COPY infra/production/console.nginx.conf /etc/nginx/nginx.conf
COPY infra/ingress/retired-console-routes.conf /etc/nginx/retired-console-routes.conf
LABEL org.opencontainers.image.version=${RELEASE_VERSION} org.opencontainers.image.title="MetaHub production console"
USER 101:101
EXPOSE 8080
ENTRYPOINT ["nginx", "-g", "daemon off;"]
