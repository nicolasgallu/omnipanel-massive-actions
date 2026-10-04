# AGENTS.md — Omnipanel

Plataforma multi-canal: inventario interno → publicaciones en **MercadoLibre** y
**Tienda Nube**, sync de stock/ventas hacia **Bitcram**. Backend Flask
(`backend/`) + front React/Vite (`frontend/`). DB MySQL en Cloud SQL (GCP),
imágenes en GCS (`pictures_ecommerce_guiaslocales`). Este archivo es la guía
para trabajar en el repo: leelo antes de tocar código. El código es la fuente
de verdad; acá solo viven reglas estables, punteros y pendientes.

## Mapa rápido

- `backend/main.py` — registra webhooks + REST API. `backend/app/` — backend completo.
- **Front**: `SKILL_FRONT.md` — protocolo obligatorio antes de tocar `frontend/src`:
  primero el "Concepto de Front" en el chat (interacción, estados, escenarios),
  aprobación del usuario, recién después código.
- Dev local: `./run.sh` (un comando, docker compose: `mysql:8.4` con seed de datos
  reales en `backend/seed_data.sql` + backend gunicorn :8080 + frontend nginx :5173).
  `./run-native.sh` = sin docker. La DB la elige `DB_HOST` en `backend/.env`
  (seteado → MySQL local; vacío → connector Cloud SQL). Re-seed:
  `docker compose down -v && ./run.sh`. Regenerar el dump: `backend/scripts/export_seed.sh`.
  Validar que el esquema local y Cloud SQL coincidan: `backend/scripts/check_schema_sync.py`
  (compara tablas/columnas/índices/FKs/vistas/triggers vía information_schema;
  exit 0 = idénticos o solo diferencias cosméticas, `--strict` las falla, `--json` para CI).
  Subir las órdenes FAKE-* (y el producto TEST) del MySQL local a Cloud SQL:
  `backend/scripts/push_fake_orders_to_cloud.py` (`--dry-run` para previsualizar,
  `--cleanup` para borrarlas de Cloud SQL; remapea cuentas por
  (platform, external_account_id) — no confía en que los ids coincidan).
- Webhooks (no tocar su contrato):
  - `backend/app/webhook/meli_dispatcher.py` → `/webhooks/meli` (una URL para todos los topics de Meli: inbox en `events` + ruteo por `topic` vía `registry.py`). El inbox SIEMPRE queda inline (antes del 200); los handlers pesados del registry se encolan vía `backend/app/tasks.py` (Cloud Tasks en prod, stub en memoria en dev/tests). `orders_v2` y `items` siguen inline.
  - `backend/app/webhook/task_worker.py` → `/internal/tasks/webhook` (worker de Cloud Tasks: OIDC en prod, corre el handler del topic; 200 ok / 500 reintento). Guía de setup: `backend/docs/cloud_tasks_setup.md`.
  - `backend/app/webhook/registry.py` → `{platform: {topic: handler}}` (nuevo platform = nuevo módulo + una entrada).
  - `backend/app/webhook/topics/` → handlers fetch+upsert (messages, price_suggestions, shipments, promotions, claims, payments, invoices) hacia tablas planas `mercadolibre.*`.
  - `backend/app/webhook/item_event.py` → sync de status de items (topic `items`).
  - `backend/app/webhook/selling_event.py` → `/webhooks/sells` (orders_v2 de Meli + Tiendanube).
- REST API del front (auth por token itsdangerous):
  - `backend/app/api/auth.py` + `auth_utils.py` (login, token, `require_auth`, `require_business`).
  - `backend/app/api/inventory.py`, `channels.py` (publicaciones ML/TN), `ai.py`, `admin.py` (empleados, prompts, settings, selling-costs).
  - `backend/app/api/oauth.py` → `GET /api/oauth/callback` (OAuth de ML/TN: plataforma por `Referer`, match de cuenta por user_id del code (ML) o hostname vs `credentials.url` (TN), intercambia code por tokens con client_id/client_secret de la fila credentials; no crea cuentas, no usa token de usuario).
  - `backend/app/api/support.py` → `/api/support/tickets` (tickets de soporte en `platform_accounts.support_tickets`, scoped por business_id).
  - `backend/app/api/platform_admin.py` → `/api/platform/*` (**panel de plataforma**, admins de `platform_accounts.admins` con token salt propio): login, businesses (listar/crear/activar-desactivar) y cuentas por business (crea el par accounts+credentials en una transacción). El OAuth callback existente completa la cuenta pendiente.
- Pipelines: `backend/app/pipelines/publish.py` (publicaciones), `backend/app/pipelines/sells.py` (órdenes → stock).
- Integraciones: `backend/app/integrations/{mercadolibre,tiendanube,bitcram,core}`.
- Front: `frontend/` (React/Vite; src/lib, src/pages, src/features, src/mock).
- Esquema DB: `backend/backend_tables.md` (actualizarlo SIEMPRE que cambie el schema).

## Invariantes (no romper)

1. **Dedup de eventos**: clave única `(account_id, source, event_type, external_id)`.
   NUNCA agregar campos a esa clave; el actor se guarda aparte (`actor_id`,
   `actor_role`). `external_id` estable = idempotencia de re-deliveries.
2. **Scoping por business**: toda consulta/acción filtra por `business_id`.
   Nunca confiar en el id que manda el front; validar ownership (ver
   `_owned_product` / `_owned_account`).
3. **Actores en `events`**: webhooks validan con `resolve_actor` (business o
   employee activo del mismo business; default = business). Eventos de sistema
   (orders, item status) = NULL. Acciones del dashboard van con `source="dashboard"`.
4. **Webhooks NO usan tokens de usuario**: Meli identifica la cuenta por el
   `user_id` del payload (via `get_account_owner`); Tiendanube por `store_id`.
   El HMAC de TN sigue siendo stub (siempre True) hasta que exista el app
   secret; el worker de Cloud Tasks usa OIDC de servicio (no tokens de negocio).
5. **Roles**: Ventas / Usuarios / Configuración son SOLO `business`. Backend:
   `@require_auth` + `@require_business` (403 a empleados). Front:
   `usePermissions()` + `<RequireBusiness>` + `businessOnly` en el NAV.
   Empleados conservan: inventario, publicación, imágenes, IA, `/api/accounts`.
6. **`reason`/`remedy` espejan la realidad**: NULL en éxito, mensaje legible en
   error (el JSON crudo va al log, no al campo). `status` de listing lo
   sincroniza el webhook de items (no inventar estados).
7. **Sells**: máquina de estados paid/cancelled, `GET_LOCK` por orden,
   AUTOCOMMIT (el POST a Bitcram es externo, no se puede rollbackear).
   `stock_movements.status`: `attempting → posted | failed | failed_ambiguous`.
   `failed_ambiguous` = solo intervención manual (evitar doble posteo).
8. **HTTP a Meli**: usar `_meli_request` (product_handler): GET/PUT reintentan
   429/5xx con backoff; POST no se reintenta (crear duplicado). Publicar jamás
   se reintenta a ciegas.

## Convenciones

- **Docs primero (regla anti shape-mismatch)**: antes de escribir código que
  consuma CUALQUIER endpoint externo (webhook/topic nuevo, GET/POST/PUT a
  MercadoLibre, Tiendanube u otra plataforma), releer SIEMPRE la documentación
  oficial de ese endpoint y citar la URL de la fuente en el comentario o el
  commit — nunca asumir la forma del JSON por el nombre de los campos ni por
  código previo. Si la doc no se puede leer por cualquier motivo: PARAR y
  preguntarle al usuario (que la provee) antes de continuar.
- **Simplicidad**: sin sobre-ingeniería. Patrones existentes del repo antes que
  frameworks nuevos. Cambios chicos y legibles.
- **Mensajes al usuario en español**, forma `{"error": "<code>", "message": "<texto>"}`.
- **Logs**: cada línea lleva `[event=N]` (correlación). Los entrypoints de
  webhooks hacen `set_event_id(None)` al inicio y `set_event_id(event_id)`
  tras el claim. Usar `logger.exception` en fallos.
- **Tests**: `cd backend && .venv/bin/pytest tests/`
  (283 tests, self-provisioning: andan sobre cualquier DB vacía; fixtures
  scratch_business/employee/product con auto-cleanup). Corren contra el MySQL
  LOCAL (`DB_HOST` en `.env`), rápido — no usan Cloud SQL. NUNCA disparar
  WhatsApp real en tests (stub de `enviar_mensaje_whapi`). Todo verde (29/09/2026).

## Pendientes conocidos

- **IMPLEMENTADO 04/10 — Acciones masivas (backend + DB + FRONT, Figma v184)**:
  selección de productos en las 3 vistas de inventario → cartel de acciones
  con contadores en vivo por plataforma ("N ready", estado LOCAL
  solo) → UN job por cuenta con FIFO por cuenta (dispatch atómico: un solo
  `running` por cuenta, cuentas distintas en paralelo) → ejecución por lotes
  idempotentes (claim_token) que ejecutan el MISMO `pipeline_publish` del
  botón individual (+ `link_catalog`/`unlink_catalog` nuevos en
  `product_handler.py`: ficha por GTIN / swap con sombra). Schema
  `mass_actions` (`jobs` + `job_items` ledger; DDL idempotente en
  `scripts/mass_actions_schema.sql`, ya aplicado al MySQL local;
  `backend_tables.md` y `check_schema_sync.py` actualizados). Código:
  `backend/app/jobs/` (eligibility/engine/api/internal) + blueprints en
  `main.py`. Worker SEPARADO (decisión del usuario), modelo PUSH:
  `mass-actions-worker/` en la raíz, carpeta autónoma estilo `load-tests/`
  (Dockerfile + cloudbuild.yaml + README), orquestador delgado: el backend
  encola UN ticket por lote en Cloud Tasks (cola `mass-actions`,
  `enqueue_mass_action()` en `tasks.py`) hacia `POST /run` del worker (OIDC,
  audience = `MASS_ACTIONS_WORKER_URL`); el worker procesa un lote vía
  `/internal/mass-actions/{next, items/<id>/execute, advance}` con Bearer
  compartido `MASS_ACTIONS_INTERNAL_TOKEN` (vacío = sin auth dev/tests) y
  Cloud Run lo escala a CERO al quedar sin tráfico. El backend encadena el
  ticket siguiente mientras quede trabajo (`engine._kick_ticket`): la cadena
  termina sola en idle; 500 = Cloud Tasks reintenta (claim atómico =
  idempotente). Dev local: `DEV_LOOP=1` en el worker (pull). API pública
  `/api/mass-actions/*` (@require_auth, empleados incluidos): eligibility,
  create (202 / 400 nothing_to_run), list (queue_position + actor), detail
  (failed_items paginados), DELETE (queued→cancelada, running→aborta;
  terminal con resultado→409). Una fila de auditoría en `events` por job.
  Reclaim oportunista (600s sin heartbeat → failed, ítems running viejos →
  pending). FRONT (port del Figma v184): barra flotante + menú de dos paneles
  + modal con checkboxes por plataforma y confirmación extra para borrar en
  `features/massActions/{shared,SelectBox,MassActionsBar}.tsx`, subpágina
  `/inventory/ejecuciones` (`pages/EjecucionesPage.tsx`: tabla, RunPill,
  drawer con contadores/errores/borrar, polling 2s mientras hay activas),
  nav Inventario → Ejecuciones (ícono nuevo en NavIcon), selección persistente
  (se limpia al cambiar de página/canal) en InventoryPage y
  ChannelListingsPage, `massDrop` en index.css, `massActionsApi` + tipos +
  contrato (`massActionsApi.test.ts`) + mock de dev. API pública del front:
  `POST /api/mass-actions/eligibility {product_ids}` →
  `{action: {ml, tn}}`; `POST /api/mass-actions {action, channel, product_ids}`
  (una corrida por llamada); `GET /api/mass-actions` / `/<id>` (MassRun:
  statuses `queued|running|done|done_errors|failed|cancelled`, `user`,
  `queue_position`, `failures[{product,reason}]`); `DELETE /<id>`.
  Tests: backend 377 passed/5 skipped; front 39 passed + typecheck + build.
  PENDIENTE DEL USUARIO: crear `mass_actions` en Cloud SQL
  con el script, deployar el worker (registry+trigger+Cloud Run
  scale-to-zero, `--no-allow-unauthenticated`, timeout=300), crear la cola
  `mass-actions` en Cloud Tasks + `roles/run.invoker` al SA, setear el token
  y `MASS_ACTIONS_WORKER_URL`/`MASS_ACTIONS_QUEUE` en ambos servicios. Spec:
  `backend/docs/mass_actions_plan.md` (contrato final).

- **IMPLEMENTADO 04/10 — Panel unificado de Envíos (backend + front, contrato
  FINAL del Figma)**: tabla `tiendanube.shipments` (1:1 con la orden, key
  `(account_id, order_id)`; `status` = estado POR FILA del enumerado único
  `pending|handling|ready_to_ship|shipped|delivered|not_delivered|cancelled`
  — TN mapea su `shipping_status` al guardar: unpacked→pending,
  unshipped/partially_packed→handling, shipped/partially_fulfilled→shipped,
  delivered→delivered, cancelada→cancelled; `data` = contexto de envío
  completo incl. `fulfillments` con `tracking_info.url`).
  `record_tn_shipment` se llama desde `record_order` (rama TN);
  `fetch_order` pide `?aggregates=fulfillment_orders` con fallback ante 4xx.
  API `GET /api/shipments` (`app/api/shipments.py`): UNION ML+TN, filtros
  `channel` + `status_group` (grupos to_prepare/in_transit/delivered/
  not_delivered/cancelled calculados por CASE) + `q`, page 0-based,
  `counts{total,to_prepare,in_transit,delivered,incidents}` (sin filtro de
  grupo) + `account_total` (estado vacío). Fila: `external_id` (ML: envío,
  TN: orden), `status` por fila, `substatus`/`logistic_type`/`mode` (ML),
  `shipping_method`/`tracking_url` (TN), `receiver`, `items[{id,title,
  quantity}]`. Backfill: `scripts/backfill.py tn-shipments` (y el push de
  FAKE-* a Cloud copia/limpia shipments). FRONT (port del Figma): página
  única `/envios` en `pages/ShipmentsPage.tsx` (buscador 250ms, filtros
  Canal/Estado, strip 5 métricas, acciones condicionales ML=Etiqueta /
  TN=Ver tracking, skeletons/error/vacío/sin-resultados, paginación), nav
  sin submenú Envios (rutas viejas redirigen), `components/ChannelBadge.tsx`
  compartido (colores de marca sólidos, sin borde) usado también en Ventas
  (tabla + drawer vía salesShared), `shipmentsApi.list` + types + test de
  contrato + mock de dev actualizados; se eliminó `channelsApi.mlShipments`.
  Tests: backend 360 passed/5 skipped; front 33 passed + typecheck.
  GCP HECHO (04/10): tabla creada en Cloud SQL + 17 envíos migrados desde
  `tiendanube.orders` (`scripts/seed_tn_shipments_from_orders.py`,
  idempotente) + API verificada contra Cloud SQL real (scoping por business
  OK: las 9 filas de la cuenta TN de Emiliano/business 231 quedan fuera del
  panel de business 1). El backfill real (`backfill.py tn-shipments`) trajo 0
  órdenes porque la tienda reconectada (8182050) NO tiene órdenes en la API
  (las históricas 20xxxxxx eran de la encarnación anterior de la tienda);
  cuando entren órdenes nuevas, el webhook llena `tiendanube.shipments` solo.
  Spec: `backend/docs/shipments_panel_plan.md` (§12 contrato final, §13 GCP).
  NOTA: `check_schema_sync.py` es MySQL-8-only (usa `IS_VISIBLE` de
  information_schema): contra un MySQL local MariaDB falla; con el docker
  mysql:8.4 oficial no aplica.
  NOTA seed: `backend/seed_data.sql` traía 2 bugs pre-existentes del dump
  (INSERT de `mercadolibre.attributes` con un valor de más de la columna
  dropeada `category_options`; credentials TN con access_token NULL) — el
  primero se corrigió en el dump; el segundo es estado real de Cloud SQL
  (los tests de acciones TN necesitan token localmente). También se corrigió
  el `external_account_id` de la cuenta TN local (estaba `307027338`, el id
  de ML; el real de Cloud es `8182050`) para que el mapeo local→Cloud de
  `push_fake_orders_to_cloud.py` funcione (verificado dry-run: 1→1, 2→92);
  el script ahora copia/limpia/verifica también `tiendanube.shipments`.

- **RESUELTO 02/10 — Shape-mismatch de handlers Meli (auditoría + docs)**: los
  handlers de topics guardaban campos vacíos por leer la respuesta de Meli con
  la forma equivocada. Corregido contra doc oficial (regla nueva: "Docs
  primero" en Convenciones): `questions` (respuesta PLANA `{text,status,from}` +
  nickname vía `GET /users/{id}`), `price_suggestions` (`current_price`/
  `suggested_price` son objetos `{amount,usd_amount}`), `handle_competition`
  (`current_price` top-level existe), `promotions` (`status` es objeto
  `{"id":...}` y la llamada usa `app_version=v2`), `claims` (la orden es
  `resource_id` con `resource=="order"`), `invoices` (orden en
  `items[].external_order_id`), `cost_calculator` (`/sites/MLA/listing_prices`
  devuelve LISTA — el cálculo de costos fallaba silencioso y nunca persistía;
  `fee_tax` no existe en la API, siempre default 21%). También: guards de
  `reply_status` tratan `''` como "sin procesar" (defaults viejos de Cloud
  SQL). Pendiente de verificar contra doc: `payments` (`order_id` de
  `/collections/{id}` y vigencia del endpoint).
- **IMPLEMENTADO 02/10 — Panel de ventas (workflow de órdenes ML/TN)**: las
  tablas `mercadolibre.orders`/`tiendanube.orders` ganaron columnas
  normalizadas (`channel_status`, `status_history` append-only, `buyer_name`,
  `buyer_external_id`, `total`, `currency`, `date_created`, `link`;
  `tiendanube.orders` además `payment_status`; ALTER para Cloud SQL documentado
  en `backend_tables.md`). `app/integrations/core/order_records.py`:
  `record_order` registra TODA orden en el webhook (cualquier estado, antes del
  filtro) con historia de estados dedupeada por `key` (raw en ML,
  `raw|payment_status` en TN). Trigger GLOBAL de descuento de stock:
  `businesses.config.stock_sync.trigger` = `paid` (default) | `confirmed` —
  la venta se dispara en `paid` o desde `confirmed`/`open` según config; la
  reversa SIEMPRE en `cancelled`/`voided`/`refunded`. La máquina de `sells.py`
  NO cambió (sigue leyendo `orders.status` como venta hecha/cancelada; el
  estado real vive en `channel_status`+`status_history`). API nueva
  `GET /api/sales/orders` (contrato final 02/10: page 0-based, `counts` sin
  filtro de status, ítems con `stock_transactions` (sale/return + comprobante)
  e `history` embebidos, estados `pending_payment|paid|delivered|cancelled` y
  sync `synced|pending|error|not_applicable` — sin `reverted`, que ahora se
  muestra como `synced`; + detalle `/<platform>/<order_id>`),
  `GET /api/sales/report` (cards + serie diaria por tz Argentina;
  `days=7|30|90&channel=`) y trigger IMS en `/api/settings/stock-sync`
  (`trigger` en GET, preservado en POST, `PATCH /settings/stock-sync/trigger`).
  Front: página `/ventas` real (port del Figma) con tabs Órdenes|Reportes en
  `frontend/src/pages/VentasPage.tsx` + `features/sales/*` +
  `components/{ListingColumnManager,StatStrip}.tsx`, y picker "Sincronización
  de ventas" en `ImsSettings.tsx` (API real, sin localStorage). Pendiente del
  usuario: correr el ALTER en Cloud SQL. Spec:
  `backend/docs/sales_panel_plan.md` (§12 con el contrato final).
- **IMPLEMENTADO 29/09 — Panel de plataforma**: tabla `platform_accounts.admins`
  (token salt propio `omnipanel-admin-auth-v1`, `require_admin`) + endpoints
  `/api/platform/*` (login, businesses listar/crear/activar-desactivar,
  cuentas por business con alta transaccional accounts+credentials). Schema:
  `businesses.active` (desactivación estricta: login bloqueado, tokens de
  business/employee mueren al instante vía chequeo por request en
  `require_auth`, webhooks internos 403 y notificaciones de Meli ignoradas),
  `accounts.external_account_id` ahora NULLable (cuenta "pendiente de OAuth").
  Admin seed: admin@guiaslocales.com (password entregada al usuario, cambiable
  con `PATCH /api/platform/password`). Pendiente: el front del panel (lo
  desarrolla el usuario aparte con el contrato de /api/platform).
- **ELIMINADO 29/09 — flujos webhook deprecados**: se eliminaron los webhooks
  internos `/webhooks/publications` y `/webhooks/images` (`publish_event.py`,
  `images_event.py`), las ramas webhook-only del pipeline (`meli_pictures` →
  `ai_images.py`, `create_template`/`create_size_grid` → `grid_size.py`),
  `prepublish()`/`_generate_category_options()` de `product_handler.py` y
  `ai_completation.py`. El dashboard quedó 100% en la REST API
  (`channels.py`/`inventory.py`); los tests del pipeline se migraron a llamadas
  directas a `pipeline_publish`.
- **IMPLEMENTADO 29/09 — Guía de talles (size grid, domain-driven)**: categorías
  que exigen SIZE_GRID_ID se resuelven automáticamente al publicar
  (`app/integrations/mercadolibre/size_grid.py`, hook en `publish`): el spec
  sale del template `technical_specs?section=grids` de cada dominio
  (main_attribute = primer `main_attribute_candidate` —ropa: SIZE, calzado:
  MANUFACTURER_SIZE—; equivalencias FILTRABLE_SIZE solo si existen; medidas =
  atributos `required` `number_unit` sin `grid_filter`, de un solo tipo de
  medida). Reusa la guía cacheada en `mercadolibre.size_grids` por (cuenta,
  dominio, marca, género) o la CREA (`POST /catalog/charts`; nombre sin "_",
  máx 60 chars, reintento por colisión). El wizard muestra el talle + las
  medidas requeridas (`GET /api/mercadolibre/size-grid/measures`) y manda las
  medidas en `config.attributes` (de ahí las lee el resolver). Validado contra
  Meli real en vestidos, zapatillas, pantalones, camisas, calzas, shorts,
  pijamas, camperas, buzos y remeras; los gorros no exigen grilla (sin
  SIZE_GRID_ID). Se dropeó `mercadolibre.attributes.category_options` y
  `mercadolibre.size_grid`; se creó `mercadolibre.size_grids`.
- **RESUELTO 28/09**: delete de ML robusto ante moderación de Meli — hace GET
  del item primero (404 → limpia la fila), saltea el PUT `closed` si ya está
  `closed`/`inactive`, y trata `item.status.not_modifiable` como "ya cerrado"
  en vez de quedar en 'Failed to Delete.' para siempre. Además: pause/update
  sincronizan el estado REAL de Meli al instante (misma lógica que el webhook
  de items) y la respuesta de las acciones incluye `meli_status` + `sub_status`
  para que el front avise cuando el item está `under_review` ("el cambio de
  estado se aplicará cuando termine la revisión").
- **RESUELTO 28/09 (moderaciones)**: el sync de items consulta la moderación
  oficial de Meli (`GET /moderations/last_moderation/{item_id}-ITM`) cuando el
  estado es `under_review`/`paused` y guarda `reason` = REASON y
  `remedy` = REMEDY legibles de Meli (JSON crudo al log). El drawer ML muestra
  la "Sugerencia de MercadoLibre" en la vista Pre-publicado. Con esto el
  usuario ve exactamente qué corregir (ej. "Corrige tus fotos: no cumple el
  tamaño mínimo, posición y proporción del producto.").
- **RESUELTO 27/09**: TN orders fetch_order (`.get("access_token")` + guard),
  `insert_and_get_id` con `lastrowid` (misma conexión) en tnube
  `_insert_record` + `inventory.upload_image`, 401 intencional para account
  desconocido (documentado en el test), guard de `calculate_cost` tras publish
  fallido, errores claros de "missing access token" en meli orders/delete/
  item_event, HTTPException handler (404 real en /api), dashboard events con
  finish/fail según resultado del pipeline.
- **Webhooks de eventos Meli — IMPLEMENTADO** (inbox en `events` +
  proyecciones `mercadolibre.*` + registry). Topics ya SUSCRITOS en DevCenter
  (04/10, el usuario los suscribió todos). Inbox: `source='mercadolibre'`,
  `event_type=topic`, `external_id=_id` (hash del body si no hay `_id`),
  insert directo `status='done'`, sin `claim()`, sin columnas nuevas.
- **OAuth callback — IMPLEMENTADO** (`GET /api/oauth/callback`): plataforma por
  `Referer`, match de cuenta (ML: user_id del code vs `external_account_id`;
  TN: hostname del Referer vs `credentials.url` normalizado), exchange de code
  por tokens (client_id/client_secret de la fila credentials). `backend/.env`
  ya setea `OAUTH_CALLBACK_URL=https://api.guiaslocales.cloud/api/oauth/callback`
  (tunnel de Cloudflare → :5173 Vite → proxy `/api` → :8080). Pendiente del
  usuario: registrar esa redirect_uri exacta en el DevCenter de ML y en Tienda
  Nube. Cuenta no matcheada = 400 + log (no crea cuentas).
- **CRÍTICO**: `SECRET_KEY` tiene default público (`omnipanel-dev-secret-change-me`);
  setearla en prod o cualquiera puede forjar tokens de cualquier business.
- Tiendanube: HMAC de webhook es stub (siempre True, `selling_event.py`).
- **RESUELTO 04/10 — prompts de IA por negocio**: `/api/ai/generate` ahora lee
  el prompt del negocio desde `ai.prompts` (`ai_generate_title` /
  `ai_generate_description`) con fallback a `prompt_defaults.py` (mismo
  comportamiento que prepublish); antes hardcodeaba sus propios `SYS_PROMPTS`
  y no respetaba lo que el negocio cargaba en Prompts AI.
- `heartbeat()` existe pero nadie lo llama (reclaim a los 900s puede robar
  eventos largos).
- Subida de imágenes requiere rol GCS (`storage.objectAdmin` sobre el bucket)
  en la service account del runtime.

## Cómo correr / testear

```bash
# TODO junto (recomendado): un solo comando con docker compose
./run.sh            # mysql:8.4 local (seed en 1er boot) + backend :8080 + frontend :5173
# re-seed desde cero:  docker compose down -v && ./run.sh
# refrescar el dump desde Cloud SQL:  backend/scripts/export_seed.sh

# backend a mano (sin docker): ./run-native.sh, o bien
cd backend && .venv/bin/python main.py     # :8080 (DB local si DB_HOST está en .env)

# tests (contra el MySQL LOCAL, rápido — el stack o un mysql local debe estar arriba)
cd backend && .venv/bin/pytest tests/

# front (dev, proxy /api → :8080)
cd frontend && pnpm dev         # o VITE_USE_MOCK=1 pnpm dev (sin backend)

# front tests (Vitest + Testing Library, contratos estables: roles/auth/api client)
cd frontend && pnpm test
```

Probar el webhook de Meli en local: `POST /webhooks/meli` con
`{"topic": "...", "user_id": <external_account_id>, "resource": "...", "_id": "..."}`
(sin HMAC; el dedup es por `_id` y el inbox queda en `platform_accounts.events`).

## Deploy (plan, aún sin implementar)

Cloud Run único con contenedor mergeado (nginx o Flask sirviendo `frontend/dist`
+ gunicorn). Env: `SECRET_KEY` (obligatorio), vars de DB y schemas; en deploy
`DB_HOST` queda VACÍO (usa el connector de Cloud SQL vía `INSTANCE_DB`). En Cloud
Run NO hace falta `GOOGLE_APPLICATION_CREDENTIALS` (ADC del service account del
runtime, que necesita Cloud SQL Client + storage.objectAdmin). Las URLs de
webhooks (`/webhooks/*`) deben permanecer estables (Meli DevCenter y el
platform interno apuntan a ellas).
