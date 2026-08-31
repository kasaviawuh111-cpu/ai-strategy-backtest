# syntax=docker/dockerfile:1.7

FROM node:22.23.2-alpine3.24 AS builder

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH

RUN corepack enable && corepack prepare pnpm@11.19.0 --activate

WORKDIR /app/web

COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile

COPY web ./

ARG VITE_API_BASE_URL=
ARG VITE_USE_MOCK=false
ARG VITE_DATA_AS_OF_DATE=2026-08-06
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_USE_MOCK=$VITE_USE_MOCK
ENV VITE_DATA_AS_OF_DATE=$VITE_DATA_AS_OF_DATE

RUN pnpm build


FROM nginxinc/nginx-unprivileged:1.28.1-alpine AS runtime

COPY deploy/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder --chown=101:101 /app/web/dist /usr/share/nginx/html

USER 101:101

EXPOSE 8080

HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 \
    CMD ["wget", "--quiet", "--output-document=-", "http://127.0.0.1:8080/healthz"]
