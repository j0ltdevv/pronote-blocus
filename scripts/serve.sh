#!/usr/bin/env bash
# Lance l'interface web avec un APP_SECRET stable.
#
# Pourquoi ce script : `uvicorn server.main:app` avec un APP_SECRET généré au
# démarrage invalide tous les comptes à chaque redémarrage (les jetons PRONOTE
# sont chiffrés avec cette clé). Ici la clé est créée une fois, rangée dans
# DATA_DIR, et réutilisée.
#
#   scripts/serve.sh              # 0.0.0.0:8020
#   PORT=8000 scripts/serve.sh    # autre port
#   HOST=127.0.0.1 scripts/serve.sh

set -euo pipefail

cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-$HOME/pronote-data}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8020}"
PYTHON="${PYTHON:-.venv/bin/python}"

mkdir -p "$DATA_DIR"
KEY_FILE="$DATA_DIR/app_secret"

if [ ! -s "$KEY_FILE" ]; then
  "$PYTHON" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  echo "APP_SECRET créé dans $KEY_FILE — sauvegarde-le hors de cette machine." >&2
fi

export APP_SECRET="$(cat "$KEY_FILE")"
export DATA_DIR
export WEB_ORIGIN="${WEB_ORIGIN:-http://localhost:$PORT}"

echo "Pronote Blocus web : http://$HOST:$PORT (données: $DATA_DIR)" >&2
exec "$PYTHON" -m uvicorn server.main:app --host "$HOST" --port "$PORT"
