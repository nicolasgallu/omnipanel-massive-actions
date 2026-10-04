# mass-actions-worker — Worker de "Acciones masivas" (deployable separado)
#
# Carpeta autónoma, separada de la app (mismo patrón que load-tests/): la
# podés extraer/pushear a tu repo tal cual. El worker es un ORQUESTADOR
# DELGADO: no toca la DB, no conoce el pipeline, no tiene tokens de usuarios.
#
# Modelo PUSH (Cloud Tasks): el backend encola un "ticket" por lote hacia
# POST /run (OIDC). El worker procesa UN lote:
#
#   POST {SERVICE}/internal/mass-actions/next            -> lote o idle
#   POST {SERVICE}/internal/mass-actions/items/<id>/execute  (por ítem)
#   POST {SERVICE}/internal/mass-actions/advance          -> heartbeat/cierre
#
# y responde 200; Cloud Run lo escala a cero al quedar sin tráfico (el push
# lo despierta). El backend encadena el ticket siguiente — la cadena termina
# sola cuando la cola queda vacía. 500 = Cloud Tasks reintenta el ticket.
#
# Dev local (sin Cloud Tasks): DEV_LOOP=1 activa un loop de pull con el
# mismo process_one_batch (el backend en stub no entrega tickets).
from flask import Flask, jsonify, request

from worker import OIDC_ENFORCED, process_one_batch, verify_oidc
from worker import log

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return jsonify({"service": "mass-actions-worker", "status": "ok"})


@app.route("/run", methods=["POST"])
def run():
    """Un ticket = un lote. 500 ante falla para que Cloud Tasks reintente."""
    if OIDC_ENFORCED and not verify_oidc():
        return jsonify({"error": "unauthorized", "message": "Token inválido"}), 401
    try:
        result = process_one_batch()
        return jsonify(result), 200
    except Exception:
        log.exception("ticket failed")
        return jsonify({"error": "batch_failed"}), 500
