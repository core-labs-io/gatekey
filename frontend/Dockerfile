# Gatekey admin console (Next.js).
# Multi-stage build: install + build in one stage, run the standalone
# Next.js output (next.config.js sets output: "standalone") in a slim
# runtime stage.

FROM node:20-slim AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund

FROM node:20-slim AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Next.js inlines NEXT_PUBLIC_* env vars into the client bundle at BUILD
# time. To keep one published image pointable at any backend host, the
# build bakes a fixed placeholder string, and docker-entrypoint.sh
# substitutes the real URL (from GATEKEY_PUBLIC_API_BASE_URL, default
# http://localhost:8000) into the built output at container start.
ARG NEXT_PUBLIC_API_BASE_URL=__GATEKEY_API_BASE_URL__
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN npm run build

FROM node:20-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
# Next.js standalone binds to $HOSTNAME, which Docker sets to the container
# id - pin it to 0.0.0.0 so the server listens on all interfaces (and the
# in-container localhost healthcheck works).
ENV HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
EXPOSE 3000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
