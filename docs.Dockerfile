# Docs site (docs.remembra.dev), built from docs/ with MkDocs Material on every deploy.
#
# Coolify app "remembra-docs": Build Pack Dockerfile, Base Directory "/",
# Dockerfile Location "/docs.Dockerfile", Ports Exposes 8080 (the server runs
# as the unprivileged nginx user; it was 80 before CTR-1). Before this file the
# app served a copy of the built site committed to site/, which went stale.
#
# Base images are pinned by digest (CTR-1); Dependabot (docker, /) proposes
# updates. The build tools are pinned by hash (.github/docs-requirements.txt).
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS build
WORKDIR /src
COPY .github/docs-requirements.txt ./docs-requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r docs-requirements.txt
COPY mkdocs.yml ./
COPY docs ./docs
COPY docs-nginx/remembra-headers.conf scripts/docs_csp.py ./
# The CSP allows each inline script of this build by its hash (LIVE-4).
RUN mkdocs build --strict --site-dir /out \
    && python docs_csp.py /out remembra-headers.conf

FROM nginx:1.30-alpine@sha256:985220252f3863977e468f611ef118ebd01421289dd86ee1ae99cb068c3bce2b
COPY docs-nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /src/remembra-headers.conf /etc/nginx/remembra-headers.conf
# Root-owned and read-only to the nginx user that serves it.
COPY --from=build /out /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf \
    && nginx -t \
    && rm -rf /tmp/*

USER nginx
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
CMD ["nginx", "-g", "daemon off;"]
