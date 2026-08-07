# Gatekey admin console (Next.js) - Phase 1.7 deployment.
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
# Baked in at build time - Next.js inlines NEXT_PUBLIC_* env vars into the
# client bundle at build time, not read at container start. See
# ../docker-compose.yml for how this is supplied as a build arg.
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN npm run build

FROM node:20-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
