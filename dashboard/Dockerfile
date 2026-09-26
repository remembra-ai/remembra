# Dashboard - Static Site Build
#
# Coolify app "remembra-dashboard": exposed port 8080 (the server runs as the
# unprivileged nginx user; it was 80 before CTR-1). Base images are pinned by
# digest; Dependabot (docker, /dashboard) proposes updates.
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source files
COPY . .

# Build args for API URL (set at build time)
ARG VITE_API_URL=https://api.remembra.dev
ENV VITE_API_URL=$VITE_API_URL

# Build the app
RUN npm run build

# Production stage - nginx, run as the unprivileged nginx user (CTR-1)
FROM nginx:1.30-alpine@sha256:985220252f3863977e468f611ef118ebd01421289dd86ee1ae99cb068c3bce2b

# The whole nginx config and the security headers it includes
COPY nginx.conf /etc/nginx/nginx.conf
COPY security-headers.conf /etc/nginx/remembra-headers.conf

# Copy built files (root-owned, read-only to the nginx user)
COPY --from=builder /app/dist /usr/share/nginx/html

RUN rm -f /etc/nginx/conf.d/default.conf \
    && nginx -t \
    && rm -rf /tmp/*

USER nginx
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1

CMD ["nginx", "-g", "daemon off;"]
