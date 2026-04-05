#!/bin/bash

# ===============================
# USO
# ===============================
CITY=$1
PORT=$2

if [ -z "$CITY" ] || [ -z "$PORT" ]; then
  echo "Uso: ./run_osrm.sh <ciudad> <puerto>"
  echo "Ejemplo: ./run_osrm.sh massachusetts 5002"
  exit 1
fi

# ===============================
# CONFIGURACIÓN
# ===============================
OSRM_DATA_DIR="./data/$CITY"
OSRM_BASE_NAME="${CITY}-latest.osrm"
DOCKER_IMAGE="osrm/osrm-backend"

# ===============================
# VALIDACIÓN DE ARCHIVOS
# ===============================
if [ ! -f "$OSRM_DATA_DIR/$OSRM_BASE_NAME" ]; then
  echo "❌ No se encontró el archivo $OSRM_BASE_NAME en $OSRM_DATA_DIR"
  exit 1
fi

# ===============================
# LEVANTAR OSRM
# ===============================
echo "🚀 Levantando OSRM en http://localhost:$PORT para '$CITY'..."
echo "Presiona Ctrl+C para detenerlo."

docker run --rm -t -i --platform linux/amd64 \
  -p $PORT:5000 \
  -v "$(pwd)/$OSRM_DATA_DIR:/data" $DOCKER_IMAGE \
  osrm-routed /data/$OSRM_BASE_NAME