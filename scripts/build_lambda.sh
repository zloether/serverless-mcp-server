#!/bin/bash
set -euo pipefail

# Usage: build_lambda.sh <lambda-dir>
# Copies source files and installs dependencies into build/<lambda-name>/,
# using Linux arm64 wheels for Lambda compatibility.

mkdir -p "$1"
LAMBDA_DIR="$(cd "$1" && pwd)"
LAMBDA_NAME="$(basename "$LAMBDA_DIR")"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="${LAMBDA_DIR}/src"
BUILD_DIR="${REPO_ROOT}/build/${LAMBDA_NAME}"
REQUIREMENTS="${LAMBDA_DIR}/requirements.txt"

mkdir -p "$SRC_DIR" "$BUILD_DIR"

cp -r "${SRC_DIR}/." "$BUILD_DIR/"
find "$BUILD_DIR" -name "__pycache__" -type d -exec rm -rf {} +

if [ ! -f "$REQUIREMENTS" ]; then
  exit 0
fi

pip install \
  -r "$REQUIREMENTS" \
  -t "$BUILD_DIR" \
  --platform manylinux2014_aarch64 \
  --only-binary=:all: \
  --python-version 3.13 \
  --upgrade \
  --quiet
