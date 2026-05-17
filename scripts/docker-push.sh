#!/bin/bash
set -e

# === 配置 ===
DOCKER_USER="${DOCKER_USER:-hfxmci}"
IMAGE_NAME="tg-lurker"
FULL_IMAGE="${DOCKER_USER}/${IMAGE_NAME}"

# === 版本信息 ===
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:-$(cat "${SCRIPT_DIR}/../VERSION" 2>/dev/null || date +%Y%m%d)}"
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BRANCH=$(git branch --show-current 2>/dev/null || echo "main")

echo "========================================"
echo "  tg-lurker Docker Build & Push"
echo "========================================"
echo "  Image:   ${FULL_IMAGE}"
echo "  Version: ${VERSION}"
echo "  Commit:  ${COMMIT}"
echo "  Branch:  ${BRANCH}"
echo "========================================"
echo ""

# === 构建 ===
echo "[1/4] Building image..."
docker build \
    --build-arg APP_VERSION="${VERSION}" \
    --build-arg APP_COMMIT="${COMMIT}" \
    -t "${FULL_IMAGE}:${VERSION}" \
    -t "${FULL_IMAGE}:latest" \
    .

echo ""
echo "[2/4] Verifying image..."
docker run --rm "${FULL_IMAGE}:${VERSION}" python -c "import config; print('OK')"

echo ""
echo "[3/4] Pushing to Docker Hub..."
docker push "${FULL_IMAGE}:${VERSION}"
docker push "${FULL_IMAGE}:latest"

echo ""
echo "[4/4] Done!"
echo ""
echo "  Pushed:"
echo "    ${FULL_IMAGE}:${VERSION}"
echo "    ${FULL_IMAGE}:latest"
echo ""
echo "  Pull command:"
echo "    docker pull ${FULL_IMAGE}:latest"
echo ""
echo "  Run command:"
echo "    docker run -d --name tg-lurker \\"
echo "      --env-file .env \\"
echo "      -v ./data:/app/data \\"
echo "      -p 8080:8080 \\"
echo "      ${FULL_IMAGE}:latest"
