# Task environment image.
#
# Copy this file to tasks/<your_task>/environment/Dockerfile and add ONLY the
# system packages and Python deps your task needs. Everything common
# (canonical dirs, tooling, keep-alive CMD) is inherited from the base image.
#
# Build (from repo root):
#   bash environments/build_task_image.sh <your_task>
#
# Data is injected at runtime by the TaskRunner (bundle + docker cp) into the
# canonical dirs (/data, /fixture, /scripts). Do NOT bake task data here.

FROM appsci-base:v0

# --- Task-specific system packages (delete if none) ---
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     <packages> \
#     && rm -rf /var/lib/apt/lists/*

# --- Task-specific Python deps (delete if none) ---
# COPY requirements.txt /tmp/requirements.txt
# RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt

# WORKDIR /work and the keep-alive CMD are inherited from appsci-base.
