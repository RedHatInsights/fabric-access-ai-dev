#!/bin/bash
set -e

cd "$(dirname "$0")"

git submodule update --init --recursive

echo "Building fabric-access-ai-dev..."
docker build -f dev-bot/Dockerfile.runner -t fabric-access-ai-dev:local .

echo "Done. Image: fabric-access-ai-dev:local"
