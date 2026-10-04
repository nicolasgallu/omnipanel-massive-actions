#!/bin/sh
# Dev local: correr el worker contra el backend local (:8080).
# Sin Cloud Tasks en local, DEV_LOOP=1 activa el pull (mismo process_one_batch
# que usa /run en prod).
#   MASS_ACTIONS_SERVICE_URL=http://localhost:8080 DEV_LOOP=1 ./run.sh
set -eu
cd "$(dirname "$0")"
export PORT="${PORT:-8090}"
exec gunicorn -c gunicorn.conf.py app:app
