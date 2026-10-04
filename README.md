# Mass Actions Worker — worker separado de "Acciones masivas" (modelo PUSH)

Orquestador **delgado** para las corridas de acciones masivas del backend
(Omnipanel). No toca la DB, no conoce el pipeline de publicación, no tiene
tokens de usuarios. Toda la lógica (eligibilidad, FIFO por cuenta, pipeline,
DB) vive en el backend, en `backend/app/jobs/`.

La carpeta es **autónoma y está separada de la app**: la podés extraer/pushear
a tu repo tal cual (mismo patrón que `load-tests/`).

## Cómo trabaja (ticket = un lote)

El backend encola un **ticket** por lote en Cloud Tasks; cada ticket es un
`POST /run` a este worker (OIDC). El worker procesa **un lote** y responde:

```
POST {SERVICE}/internal/mass-actions/next            -> lote claimado o idle
POST {SERVICE}/internal/mass-actions/items/<id>/execute   (por ítem)
POST {SERVICE}/internal/mass-actions/advance          -> heartbeat / cierre / encadenar próximo ticket
```

- **Sin loop, sin idle, sin polling:** el push de Cloud Tasks despierta al
  worker; al quedar sin tráfico, Cloud Run lo escala a **cero** (min=0).
- **Encadenado:** el backend pide el ticket siguiente mientras quede trabajo;
  la cadena termina sola cuando la cola queda vacía.
- **Retries gratis:** si el worker responde 500 (o muere a mitad del lote),
  Cloud Tasks reintenta el ticket con backoff. Un ticket duplicado es
  inofensivo: el claim atómico (`claim_token`) impide ejecutar dos veces el
  mismo ítem.
- El claim reparte trabajo entre tickets concurrentes (dos eventos masivos a
  la vez = dos tickets en paralelo, cuentas distintas).

## Variables de entorno

| Variable | Qué hace | Default |
|---|---|---|
| `MASS_ACTIONS_SERVICE_URL` | URL base del backend | — (obligatoria) |
| `MASS_ACTIONS_INTERNAL_TOKEN` | Secreto compartido con el backend (mismo valor en ambos; vacío = sin auth, solo dev) | — |
| `MASS_ACTIONS_VERIFY_AUTH` | `1` = validar OIDC de Cloud Tasks en `/run` (prod) | `0` |
| `MASS_ACTIONS_WORKER_URL` | URL pública de ESTE worker (audience del OIDC) | — |
| `ITEM_TIMEOUT` | Timeout por ítem (segundos) | `300` |
| `DEV_LOOP` | `1` = loop de pull para dev local (sin Cloud Tasks) | `0` |
| `IDLE_SECONDS` | Espera del dev loop entre pulls | `2` |
| `PORT` | Puerto HTTP (healthcheck) | `8080` |

## Dev local

```bash
# 1) backend corriendo en local (:8080), con MASS_ACTIONS_INTERNAL_TOKEN vacío
# 2) acá (el pull reemplaza a Cloud Tasks, que no existe en local):
MASS_ACTIONS_SERVICE_URL=http://localhost:8080 DEV_LOOP=1 ./run.sh
# healthcheck: curl localhost:8090/healthz
```

## Setup en GCP (manual, como load-tests)

1. **Registry + trigger de build**: editar `cloudbuild.yaml` (`_IMAGE` = tu
   registry) y correr:
   `gcloud builds submit --config mass-actions-worker/cloudbuild.yaml mass-actions-worker/`
2. **Deploy (Cloud Run SERVICE, scale-to-zero — SIN min-instances):**

```bash
gcloud run deploy mass-actions-worker \
  --image=us-central1-docker.pkg.dev/TU-PROYECTO/mass-actions/worker:latest \
  --region=us-west1 \
  --timeout=300 --memory=512Mi --cpu=1 \
  --no-allow-unauthenticated \
  --set-env-vars="MASS_ACTIONS_SERVICE_URL=https://tu-servicio.run.app,\
MASS_ACTIONS_WORKER_URL=https://mass-actions-worker-XXXX-uw.a.run.app,\
MASS_ACTIONS_VERIFY_AUTH=1" \
  --set-secrets=MASS_ACTIONS_INTERNAL_TOKEN=mass-actions-internal-token:latest
```

   - `--no-allow-unauthenticated` + OIDC: solo Cloud Tasks puede llamarlo.
   - El URL real (`MASS_ACTIONS_WORKER_URL`) lo da el propio deploy; re-deploy
     con ese valor exacto (es la audience del token OIDC).

3. **Cola de Cloud Tasks** (el backend encola acá; crear manual o con):

```bash
gcloud tasks queues create mass-actions --location=us-south1 \
  --max-attempts=5 --max-retry-duration=600s
```

   Y darle al service account de Cloud Tasks permiso de invocar el worker:
   en la consola del worker → Permissions → `roles/run.invoker` al
   `TASKS_OIDC_SERVICE_ACCOUNT` del backend (el mismo que usa meli-webhooks).

4. **Backend (prod)**: setear `MASS_ACTIONS_WORKER_URL` (URL del worker),
   `MASS_ACTIONS_QUEUE` (nombre completo de la cola) y
   `MASS_ACTIONS_INTERNAL_TOKEN` (mismo secreto que el worker).

## Seguridad / límites

- `/run` valida el token OIDC (audience = URL del worker) en prod; los
  endpoints internos del backend exigen el secreto compartido.
- El worker no maneja datos de negocio: solo ids. Cada llamada al servicio
  re-valida jobs/ítems contra la DB (scoping por business).
- Concurrencia: varios tickets en paralelo son seguros (claim atómico);
  escalar = más instancias o `--concurrency` en Cloud Run.
