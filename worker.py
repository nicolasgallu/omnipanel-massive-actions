"""Lógica del worker de acciones masivas (un lote por ticket).

`process_one_batch()` es el corazón: pide el próximo lote al servicio, ejecuta
cada ítem (el servicio hace el trabajo real contra la plataforma) y cierra con
advance. El servicio reparte entre tickets concurrentes gracias al claim
atómico por `claim_token` — un ticket duplicado (retry de Cloud Tasks) es
inofensivo.

OIDC: Cloud Tasks llama a /run con un token OIDC cuya audience es la URL del
worker (MASS_ACTIONS_WORKER_URL). En dev OIDC queda desactivado salvo que se
setee MASS_ACTIONS_VERIFY_AUTH=1.
"""
import logging
import os
import threading
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
log = logging.getLogger("mass-actions-worker")

SERVICE_URL = (os.getenv("MASS_ACTIONS_SERVICE_URL") or "").rstrip("/")
TOKEN = os.getenv("MASS_ACTIONS_INTERNAL_TOKEN") or ""
OIDC_ENFORCED = os.getenv("MASS_ACTIONS_VERIFY_AUTH") == "1"
WORKER_URL = os.getenv("MASS_ACTIONS_WORKER_URL") or ""
ITEM_TIMEOUT = float(os.getenv("ITEM_TIMEOUT", "300"))
IDLE_SECONDS = float(os.getenv("IDLE_SECONDS", "2"))


def _headers():
    return {"Authorization": "Bearer " + TOKEN} if TOKEN else {}


def _post(path, timeout, json=None):
    return requests.post(SERVICE_URL + path, headers=_headers(),
                         json=json, timeout=timeout)


def verify_oidc():
    """Valida el Authorization: Bearer <token OIDC de Cloud Tasks> con la
    audience = MASS_ACTIONS_WORKER_URL (la URL pública del worker)."""
    try:
        from google.auth.transport import requests as gauth_requests
        from google.oauth2 import id_token

        header = request_header_auth()
        if not header:
            return False
        claims = id_token.verify_oauth2_token(
            header, gauth_requests.Request(), audience=WORKER_URL)
        return claims is not None
    except Exception:
        log.exception("Invalid OIDC token on /run")
        return False


def request_header_auth():
    from flask import request
    h = request.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else None


def process_one_batch():
    """Un lote completo: next -> execute* -> advance. Devuelve el resumen."""
    if not SERVICE_URL:
        raise RuntimeError("Falta MASS_ACTIONS_SERVICE_URL")
    resp = _post("/internal/mass-actions/next", timeout=60)
    if resp.status_code == 401:
        raise RuntimeError("Token interno inválido (401 en /next)")
    if resp.status_code != 200:
        raise RuntimeError("next -> HTTP %d" % resp.status_code)
    batch = resp.json() or {}
    if batch.get("idle"):
        return {"idle": True}
    job_id = batch.get("job_id")
    items = batch.get("items") or []
    outcomes = {}
    for it in items:
        try:
            r = _post("/internal/mass-actions/items/%d/execute"
                      % int(it["item_id"]), timeout=ITEM_TIMEOUT)
            outcomes[it["item_id"]] = (r.json() or {}).get("item_status")
            log.info("item=%s -> %s", it["item_id"], outcomes[it["item_id"]])
        except Exception:
            log.exception("execute item=%s failed", it)
            outcomes[it["item_id"]] = "execute_error"
    adv = _post("/internal/mass-actions/advance", timeout=60,
                json={"job_id": job_id})
    log.info("batch job=%s items=%d advance=%s", job_id, len(items),
             adv.status_code)
    return {"ok": True, "job_id": job_id, "items": len(items),
            "outcomes": outcomes}


def dev_loop():
    """Solo DEV_LOOP=1 (sin Cloud Tasks localmente): pull continuo con el
    mismo process_one_batch. En prod NO se usa: el push despierta al worker."""
    log.info("dev loop started (service=%s)", SERVICE_URL)
    while True:
        try:
            result = process_one_batch()
            if result.get("idle"):
                time.sleep(IDLE_SECONDS)
        except Exception:
            log.exception("dev loop error")
            time.sleep(5)


def start_dev_loop():
    t = threading.Thread(target=dev_loop, name="dev-loop", daemon=True)
    t.start()
