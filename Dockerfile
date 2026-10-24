# syntax=docker/dockerfile:1
# Downflow backend + bgutil PO-token provider in one image (works on Render "Docker" services).
ARG BGUTIL_VERSION=2.0.2

# --- build the PO token provider server (must match the pip plugin version) --------------------
FROM node:22-slim AS pot
ARG BGUTIL_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 make g++ pkg-config \
        libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${BGUTIL_VERSION}" https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /src
WORKDIR /src/server
RUN npm ci && npx tsc && npm prune --omit=dev

# --- runtime -----------------------------------------------------------------------------------
FROM python:3.12-slim
ARG BGUTIL_VERSION
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DOWNFLOW_AUTO_UPDATE=stable \
    DOWNFLOW_POT_SERVER=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 \
    && rm -rf /var/lib/apt/lists/*
# Node doubles as the JavaScript runtime yt-dlp needs for YouTube signature/n-challenge solving.
COPY --from=pot /usr/local/bin/node /usr/local/bin/node
COPY --from=pot /src/server /opt/bgutil/server

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt "bgutil-ytdlp-pot-provider==${BGUTIL_VERSION}"
COPY . .
RUN chmod +x start.sh && mkdir -p data cookies
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s CMD curl -fs "http://127.0.0.1:${PORT:-8000}/" >/dev/null || exit 1
CMD ["./start.sh"]
