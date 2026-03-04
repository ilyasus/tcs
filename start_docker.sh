#!/usr/bin/env sh
set -eu

IMAGE_NAME="twc-tracker"
CONTAINER_NAME="twc-tracker"

# Defaults (can be overridden by exporting env vars before running this script)
TWC_BASE_URL="${TWC_BASE_URL:-http://192.168.1.167}"
APP_TIMEZONE="${APP_TIMEZONE:-America/Los_Angeles}"
APP_RATE_PLAN="${APP_RATE_PLAN:-EV2-A}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-15}"
TWC_TIMEOUT_SECONDS="${TWC_TIMEOUT_SECONDS:-4}"

echo "Building image: ${IMAGE_NAME}"
docker build -t "${IMAGE_NAME}" .

echo "Stopping old container (if exists): ${CONTAINER_NAME}"
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

echo "Starting container: ${CONTAINER_NAME}"
docker run -d \
  --name "${CONTAINER_NAME}" \
  -p 8080:8080 \
  -e TWC_BASE_URL="${TWC_BASE_URL}" \
  -e APP_TIMEZONE="${APP_TIMEZONE}" \
  -e APP_RATE_PLAN="${APP_RATE_PLAN}" \
  -e POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS}" \
  -e TWC_TIMEOUT_SECONDS="${TWC_TIMEOUT_SECONDS}" \
  -e APP_DB_PATH=/data/tesla_wall_charger.db \
  -v "$(pwd)/data:/data" \
  --restart unless-stopped \
  "${IMAGE_NAME}"

echo "Container started. Open: http://localhost:8080"
