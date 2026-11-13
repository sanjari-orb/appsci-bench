#!/usr/bin/env bash
# Build the shared base image, then a task's own environment image.
#
# Usage: bash environments/build_task_image.sh <task_id> [image_tag]
#
# The task must provide tasks/<task_id>/environment/Dockerfile (see
# tasks/_TEMPLATE/environment/ for a starting point).
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: bash environments/build_task_image.sh <task_id> [image_tag]"
  exit 1
fi

TASK_ID="$1"
IMAGE_TAG="${2:-appsci-task-${TASK_ID//_/-}:v0}"

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV_DIR="${REPO_ROOT}/tasks/${TASK_ID}/environment"

if [[ ! -f "${ENV_DIR}/Dockerfile" ]]; then
  echo "No Dockerfile found at ${ENV_DIR}/Dockerfile"
  echo "Scaffold one from tasks/_TEMPLATE/environment/."
  exit 1
fi

# 1. Shared base image (context = repo root so the base can COPY its helpers).
docker build -f "${REPO_ROOT}/environments/base/Dockerfile" -t appsci-base:v0 "${REPO_ROOT}"

# 2. Task image (context = the task's environment dir).
docker build -f "${ENV_DIR}/Dockerfile" -t "${IMAGE_TAG}" "${ENV_DIR}"

echo "Built ${IMAGE_TAG}"
