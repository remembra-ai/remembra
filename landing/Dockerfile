# remembra.dev: the static site on nginx, with its own config.
#
# Coolify: build pack "Dockerfile", base directory /landing, Dockerfile
# location /Dockerfile, exposed port 8080. See docs/DEPLOYING.md.
FROM nginx:stable-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY remembra-headers.conf /etc/nginx/remembra-headers.conf
# Root-owned and read-only to the nginx user that serves it.
COPY . /usr/share/nginx/html

# The web root holds only the site: drop the build files that came with it.
RUN cd /usr/share/nginx/html \
    && rm -f nginx.conf remembra-headers.conf Dockerfile .dockerignore .gitignore package.json \
    && rm -f /etc/nginx/conf.d/default.conf \
    && nginx -t \
    && rm -rf /tmp/*

USER nginx
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
CMD ["nginx", "-g", "daemon off;"]
