#!/bin/bash
set -e

# === 配置 ===
DOCKER_USER="${DOCKER_USER:-your-username}"
IMAGE_NAME="tg-lurker"
FULL_IMAGE="${DOCKER_USER}/${IMAGE_NAME}"

# === 版本信息 ===
VERSION="${1:-$(date +%Y%m%d)}"
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

echo "========================================"
echo "  tg-lurker Multi-Arch Build & Push"
echo "========================================"
echo "  Image:     ${FULL_IMAGE}"
echo "  Version:   ${VERSION}"
echo "  Commit:    ${COMMIT}"
echo "  Platforms: linux/amd64, linux/arm64"
echo "========================================"
echo ""

# 确保 buildx builder 存在
docker buildx inspect tg-lurker-builder >/dev/null 2>&1 || \
    docker buildx create --name tg-lurker-builder --use

docker buildx use tg-lurker-builder

echo "[1/2] Building & pushing multi-arch image..."
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --build-arg APP_VERSION="${VERSION}" \
    --build-arg APP_COMMIT="${COMMIT}" \
    -t "${FULL_IMAGE}:${VERSION}" \
    -t "${FULL_IMAGE}:latest" \
    --push \
    .

echo ""
echo "[2/2] Done!"
echo ""
echo "  Pushed (multi-arch):"
echo "    ${FULL_IMAGE}:${VERSION}"
echo "    ${FULL_IMAGE}:latest"
