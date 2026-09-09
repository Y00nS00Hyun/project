# React production build served by Nginx, which also reverse-proxies /api.
#
# Two stages so Node and node_modules never reach the runtime image: what ships
# is static files plus Nginx.

# ---------------------------------------------------------------------------
# Build stage
# ---------------------------------------------------------------------------
FROM node:22.21.1-alpine AS build

WORKDIR /app

# npm ci, not npm install: it installs exactly what package-lock.json pins and
# fails if the lockfile and package.json disagree, so a deployed bundle is
# reproducible.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# Built without VITE_DEBUG_USER_ID and with mode=production, so
# `import.meta.env.DEV` is statically false and the development identity header
# is eliminated as dead code. VITE_API_BASE_URL keeps its "/api/v1" default:
# the browser talks to the same origin it loaded the page from, and never to
# the backend port directly.
RUN npm run build

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
# nginx-unprivileged runs as a non-root user and listens on 8080; the stock
# nginx image needs root to bind 80.
FROM nginxinc/nginx-unprivileged:1.29-alpine

COPY --chown=nginx:nginx nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build --chown=nginx:nginx /app/dist /usr/share/nginx/html

EXPOSE 8080
