# ExpressYa · prototipo del diseño físico (Semana 3) · Equipo 02 · Dinamita

Primer incremento de construcción que pide la revisión de la Semana 2: **inventario persistido por tienda, reserva de
300 segundos y prueba automatizada de la última unidad**, más el flujo de pago con pasarela simulada y la reparación de
un evento de pago perdido. Implementa las decisiones del SDD (Equipo_02_Diseño_Semana_03): monolito modular en
FastAPI + servicio de Pagos aparte + PostgreSQL 16 (administrado en DigitalOcean en producción). Sin Redis, sin broker,
sin Kubernetes.

## Estructura

```
db/core/001_esquemas_y_tablas.sql     DDL del monolito (un esquema por módulo)
db/core/002_funciones_inventario.sql  Autoridad de inventario: reservar, confirmar (R-D02), liberar, expirar, movimientos
db/core/003_datos_demo.sql            Datos mínimos de prueba (no son datos de ExpressYa)
db/pagos/001_pagos.sql                DDL del servicio de Pagos
core/app/…                            API (FastAPI) y worker del monolito (M1, M2, M3, M5, M6, M7, C0)
pagos/app/…                           API, pasarela simulada y worker de Pagos (M4)
openapi/*.yaml                        Contratos físicos (OpenAPI 3.1)
tests/sql/…                           Pruebas contra PostgreSQL (incluye 100 clientes concurrentes con pgbench)
tests/test_*.py                       Pruebas automatizadas de la aplicación (pytest)
.do/app.yaml                          Especificación de despliegue en DigitalOcean App Platform (diseño del SDD)
infra/azure/*.sh                      Ambiente de PRUEBA en Azure for Students (Container Apps + PostgreSQL Flexible)
db/migrar.py, db/Dockerfile           Migrador para bases administradas (sin superusuario)
.github/workflows/ci.yml              CI/CD: pruebas SQL + pytest con evidencia, imágenes en ghcr.io y despliegue
```

## Ejecutar en local

```bash
cp .env.example .env
docker compose up --build            # core en :8000 (/docs), pagos en :8001
```

## Pruebas

```bash
# 1) Pruebas SQL (solo requieren PostgreSQL 16 y sus clientes psql/pgbench)
PGHOST=localhost PGUSER=postgres ./tests/sql/correr_pruebas_sql.sh

# 2) Pruebas de la aplicación (requieren Python 3.12 y PostgreSQL accesible)
pip install -r core/requirements.txt pytest
TEST_PG_ADMIN_URL=postgresql://postgres:solo-desarrollo@localhost:5432/postgres pytest -q tests
```

## Estado (evidencia, impedimentos y plan inmediato)

| Elemento | Estado |
|---|---|
| DDL del monolito y de Pagos | Cargado y verificado en PostgreSQL 16.15 |
| Funciones de la autoridad (reserva, R-D02, idempotencia, expiración, sobrecompromiso) | **Pruebas SQL ejecutadas y aprobadas** (`tests/sql`, TP-S3-04 a TP-S3-11), tres corridas |
| Última unidad: 100 clientes concurrentes | **Ejecutada con pgbench:** 1 aceptada, 99 AGOTADO, disponibilidad 0 |
| API FastAPI, workers y pruebas pytest | Código completo y compilado (`py_compile`). **Las pruebas pytest no se ejecutaron en el entorno donde se generó** porque el acceso a PyPI estaba bloqueado. El equipo debe correrlas localmente (comando de arriba) y adjuntar la salida como evidencia. |
| Faltantes con validación del supervisor, conteos, recepción y devoluciones | Diseñados en el SDD; **no implementados** en este incremento |
| Plan inmediato | 1) correr `pytest` y `docker compose`; 2) implementar ReportarFaltante / ValidarFaltante; 3) desplegar en App Platform (staging) |

## Despliegue de prueba en Azure for Students

Ambiente para probar el prototipo "en producción" sin usar una computadora propia y dentro del crédito de estudiante.
**No cambia el diseño del SDD** (producción sigue siendo DigitalOcean App Platform, ADR-21); es un ambiente de prueba
con las mismas piezas: monolito `core`, servicio `pagos` sin ruta pública, dos workers y PostgreSQL 16 administrado.

| Pieza del SDD (§31) | En Azure | Notas |
|---|---|---|
| core (2 instancias) | Container App `core`, ingreso público, 0 a 2 réplicas | Escala a cero sin tráfico: la primera solicitud tarda unos segundos |
| pagos (interno) | Container App `pagos`, ingreso **interno** | Solo accesible desde el ambiente |
| core-worker | Job programado cada 2 min (`correr_worker.py`, una pasada) | El vencimiento de reservas no depende del barrido (ADR-04) |
| pagos-worker | Job programado cada 5 min | La alerta a Finanzas puede llegar hasta 5 min después de los 30 |
| Managed PostgreSQL 16 | Azure Database for PostgreSQL Flexible Server 16, B1ms, respaldos 7 días | Dos bases y dos roles dueños (mínimo privilegio) |
| Secretos | Secretos de Container Apps (generados por `crear.sh`, nunca impresos) | |

Diferencias asumidas solo para este ambiente (supuestos técnicos): ST-AZ-01 workers como jobs de 2 y 5 min en lugar de
procesos continuos; ST-AZ-02 `TIMEOUT_PAGOS_S=30` porque `pagos` puede estar en cero réplicas; ST-AZ-04 antes de cobrar, `core` consulta `/salud` de `pagos` (hasta 60 s) para despertarlo; sin esto el primer cobro tras un periodo inactivo quedaba INCIERTO por el arranque en frío (observado el 15-nov-2026); ST-AZ-03 la base acepta
conexiones solo desde servicios de Azure y con TLS (en DigitalOcean serían "fuentes de confianza" de la VPC).

### Pasos

1. **GitHub:** sube este repositorio a la rama `main`. El workflow `ci` corre las pruebas (SQL, migrador y pytest),
   guarda la salida como artefacto `evidencia-pruebas` y publica las imágenes en `ghcr.io`.
2. **Token para leer imágenes:** GitHub → Settings → Developer settings → Personal access tokens (classic) →
   permiso `read:packages` únicamente.
3. **Crear el ambiente** en [Azure Cloud Shell](https://shell.azure.com) (Bash, en el navegador):
   ```bash
   git clone https://github.com/<usuario>/<repositorio>.git && cd <repositorio>
   GHCR_OWNER=<usuario-en-minúsculas> bash infra/azure/crear.sh        # ~15 min; pide el token sin mostrarlo
   ```
   Si la región está bloqueada para tu suscripción: `LOC=<otra-región> GHCR_OWNER=... bash infra/azure/crear.sh`.
4. **Despliegue automático (opcional):** `REPO=<usuario>/<repositorio> bash infra/azure/conectar_github.sh` y guarda en
   GitHub los valores que imprime. Desde entonces cada push a `main` que pase las pruebas se despliega solo.
   Sin este paso: `GHCR_OWNER=... TAG=latest bash infra/azure/actualizar.sh` en Cloud Shell.
5. **Probar:** `bash infra/azure/token.sh CLIENTE` y usa el token en `https://core.<dominio>/docs` (botón Authorize).
6. **Apagar:** `bash infra/azure/borrar.sh` elimina todo (base incluida).

### Costo estimado

| Concepto | Consumo estimado al mes | Gratis incluido |
|---|---|---|
| PostgreSQL B1ms + 32 GB | 730 h | 750 h B1ms, 32 GB y 32 GB de respaldo, 12 meses (Azure for Students) |
| core-worker (21 600 ejecuciones × ~10 s × 0.25 vCPU) | ≈ 54 000 vCPU-s | Container Apps: 180 000 vCPU-s, 360 000 GiB-s y 2 M solicitudes por suscripción |
| pagos-worker (8 640 × ~10 s × 0.25 vCPU) | ≈ 22 000 vCPU-s | (mismo grant) |
| core y pagos (≈ 20 h de pruebas) | ≈ 36 000 vCPU-s | (mismo grant) |
| Log Analytics | Pocos MB de logs | Se descuenta del crédito si lo hubiera |

Los ~10 s por ejecución son una estimación: revisa **Cost Management** en el portal la primera semana y el saldo del
crédito en microsoftazuresponsorships.com. Si los paquetes de ghcr.io son privados, borra versiones viejas para no
pasar la cuota gratuita de almacenamiento de GitHub Packages.

## C3 · Surtido, discrepancia y cancelación parcial (M5 Operación)

| Endpoint | Rol | Qué hace |
|---|---|---|
| `POST /v1/pedidos/{id}/preparacion` | PICKER, SUPERVISOR, ENCARGADO | PAGADO → EN_PREPARACION |
| `POST /v1/lineas/{id}/surtido` | PICKER | Surte unidades; descuenta vendible y comprometido (ADR-02) |
| `POST /v1/lineas/{id}/faltantes` | PICKER | Reporta faltante; abre discrepancia y bloquea la venta del SKU |
| `POST /v1/faltantes/{id}/validacion` | SUPERVISOR (≠ picker) | Valida: cancelación parcial, reembolso único de la mercancía (K4), conteo dirigido |
| `POST /v1/tiendas/{t}/existencias/{sku}/conteos` | PICKER, SUPERVISOR, ENCARGADO | Conteo físico; con diferencia abre discrepancia, bloquea y pide ajuste |
| `POST /v1/ajustes-inventario/{id}/autorizacion` | SUPERVISOR, ENCARGADO (≠ solicitante) | Aplica o rechaza el ajuste |
| `GET /v1/tiendas/{t}/existencias/{sku}/historial` | AUDITOR, SUPERVISOR, ENCARGADO | Quién, qué, cuándo, por qué y pedido afectado; reconstrucción del saldo |
| `GET /v1/pedidos/{id}/historial` | CLIENTE (propio), ATENCION, SUPERVISOR, AUDITOR | Faltantes, cancelaciones, movimientos, avisos y eventos del pedido |

Pruebas: `tests/sql/pruebas_c3.sql` (TP-C3-01 a 03), `tests/test_c3.py` y, en Azure, `bash infra/azure/probar_c3.sh`.

## Interfaz web de demostración (`/app`)

El mismo contenedor `core` sirve una pantalla en `/app` (sin servicios ni costo adicional):

- **Cliente**: registro **simulado** (nombre, correo y código postal; sin contraseña), catálogo M1 por código postal
  (`GET /v1/catalogo`), carrito, compra con reserva de 300 s y cuenta regresiva, pago simulado y "Mis pedidos" con
  avisos de cancelación parcial y reembolso.
- **Tienda**: picker (bandeja, iniciar preparación, surtir, reportar faltante), supervisor (validar faltantes, conteo
  físico, autorizar ajustes) y auditor (historial de existencia con reconstrucción).

La sesión de demostración (`POST /v1/demo/sesion`) emite un JWT por rol y **responde 404 cuando `AMBIENTE=produccion`**;
el inicio de sesión real queda fuera del alcance actual (proveedor de identidad previsto en el SDD).

### Cliente en tiempo real (ADR-29 propuesto, rama `cliente-tiempo-real`)

Decisión del equipo: **la Tienda y el acceso administrativo siguen simulados** (sin contraseñas ni rol root, para no
comprometer la integridad del proyecto); **el cliente se comporta como en operación real**:

- Portada con dos accesos (Cliente / Personal de tienda) y logo (`GET /app/logo.png`).
- Cantidad manual con − / + o escrita (máximo = disponible y 99); el carrito marca en rojo la línea exacta que no se pudo apartar.
- **Bitácora del pedido en vivo**: `GET /v1/pedidos/{id}/bitacora` (API-PED-10) lee `comun.outbox`, que se escribe en la
  misma transacción que cada cambio, así que el cliente ve reserva, pago, preparación, faltantes y reembolsos sin esperar
  al worker. No expone identificadores del personal, evidencia ni correlación. Nuevos eventos `PagoSolicitado` y
  `PagoResultado`; índice en la migración `007_bitacora_cliente.sql`.
- La pantalla consulta cada 3 s (pedido abierto), 10 s (mis pedidos) y 15 s (catálogo) **solo con la pestaña visible**,
  y deja de consultar cuando el pedido ya no tendrá cambios o a los 15 min (cada consulta mantiene encendido `core`).
- La cuenta regresiva de 5 min se calcula con el reloj del servidor, no con el del teléfono.

### Auditoría del ambiente de Azure (solo lectura)

```bash
bash infra/azure/auditar.sh             # recursos, versión (SHA), escalado, secretos (solo nombres), jobs, métricas de
                                        # PostgreSQL, latencia/TLS, existencias de prueba y errores recientes
bash infra/azure/auditar.sh --consumo   # además, proyección de la cuota gratuita (consumo.sh 24)
```

No crea, cambia ni borra recursos y no imprime valores de secretos. El informe queda en `infra/azure/auditoria_azure_<fecha>.txt`
y termina con la lista de avisos.

## Respuesta a la retroalimentación del 24-nov (caso «lógico 3, físico 2»)

**El caso del profesor de punta a punta** (`tests/test_caso_zeta.py`, `infra/azure/probar_zeta.sh`):
1. El encargado prepara el escenario: «Producto Zeta» con inventario lógico de 3 en la Dark Store 5 (CP 53400)
   (`POST /v1/demo/escenario-zeta`, reposición auditada con movimiento; no existe en producción).
2. El cliente pide 3 y paga. 3. El picker ve producto, pedidas, surtidas, por validar y **por surtir** (calculadas en el
   servidor) y pulsa **«Encontré menos…» → 2**: `POST /v1/lineas/{id}/surtido-verificado` (API-OPS-07) surte 2 y reporta
   1 faltante **en una sola transacción** (migración 008); la venta de Zeta queda bloqueada (ADR-27).
4. El supervisor —otra persona (INV-19)— valida: se cancela 1 unidad, se solicita el reembolso de $20.00 (ADR-28),
   el pedido queda **PARCIAL** y el inventario queda lógico 0 = físico 0, con conteo dirigido programado.
5. La bitácora registra pago, surtido, faltante, cancelación y reembolso; la **vista del personal**
   (`/v1/pedidos/{id}/bitacora?vista=personal`) muestra **quién y cuándo** (nombre y rol de la sesión simulada, correlación).

**Rendimiento y volumen** (`infra/carga/carga.py`, solo biblioteca estándar):
- Cada respuesta trae `Server-Timing: app;dur=…`. `carga.py latencia` separa tiempo **en el servidor** de **red + TLS +
  arranque en frío**; córrelo desde Cloud Shell y desde la red de la escuela para comparar.
- `carga.py lectura` (consultas concurrentes) y `carga.py checkout` (reservas concurrentes sin pagar; comprueba que no haya
  sobreventa). El CI lo ejecuta en cada cambio contra el core real con PostgreSQL 16 y guarda `evidencia/carga.txt`.

```bash
python3 infra/carga/carga.py --url https://<core> latencia
python3 infra/carga/carga.py --url https://<core> todo -n 200 -c 15    # límites: 2 000 solicitudes, 30 concurrentes
bash infra/azure/probar_zeta.sh                                         # caso Zeta en Azure con evidencia
```
