# Dashboard - Static Site Build
FROM node:20-alpine AS builder

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

# Production stage - nginx
FROM nginx:alpine

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built files
COPY --from=builder /app/dist /usr/share/nginx/html

# Expose port
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
