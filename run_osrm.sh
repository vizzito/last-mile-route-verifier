#!/bin/bash
# run_osrm.sh
# -----------
# Starts a local OSRM routing server using Docker.
# Pre-processed map data must already exist in ./data/<region>/ (see README.md).
#
# Usage:
#   ./run_osrm.sh <region> <port>
#
# Examples:
#   ./run_osrm.sh massachusetts 5002
#   ./run_osrm.sh new-york 5002
#
# Press Ctrl+C to stop the server.

set -euo pipefail

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
REGION="${1:-}"
PORT="${2:-}"

if [[ -z "$REGION" || -z "$PORT" ]]; then
  echo "Usage: ./run_osrm.sh <region> <port>"
  echo "Example: ./run_osrm.sh massachusetts 5002"
  exit 1
fi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR="./data/${REGION}"
OSRM_FILE="${REGION}-latest.osrm"
DOCKER_IMAGE="osrm/osrm-backend"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if [[ ! -f "${DATA_DIR}/${OSRM_FILE}" ]]; then
  echo "ERROR: OSRM file not found: ${DATA_DIR}/${OSRM_FILE}"
  echo "Run the extract/partition/customize steps first (see README.md)."
  exit 1
fi

# ---------------------------------------------------------------------------
# Start OSRM
# ---------------------------------------------------------------------------
echo "Starting OSRM for region '${REGION}' on http://localhost:${PORT} ..."
echo "Press Ctrl+C to stop."
echo ""

docker run --rm -it \
  --platform linux/amd64 \
  -p "${PORT}:5000" \
  -v "$(pwd)/${DATA_DIR}:/data" \
  "${DOCKER_IMAGE}" \
  osrm-routed "/data/${OSRM_FILE}"
