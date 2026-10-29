FROM nginx:1.28-alpine

COPY infra/ingress/nginx.conf /etc/nginx/nginx.conf
COPY infra/ingress/retired-console-routes.conf /etc/nginx/retired-console-routes.conf

USER 101:101
EXPOSE 8080
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
