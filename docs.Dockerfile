# Docs site (docs.remembra.dev), built from docs/ with MkDocs Material on every deploy.
#
# Coolify app "remembra-docs": Build Pack Dockerfile, Base Directory "/",
# Dockerfile Location "/docs.Dockerfile", Ports Exposes 80. Before this file the
# app served a copy of the built site committed to site/, which went stale.
FROM python:3.11-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir "mkdocs==1.6.1" "mkdocs-material==9.7.7" "pymdown-extensions==12.1"
COPY mkdocs.yml ./
COPY docs ./docs
RUN mkdocs build --strict --site-dir /out

FROM nginx:1.27-alpine
COPY --from=build /out /usr/share/nginx/html
EXPOSE 80
