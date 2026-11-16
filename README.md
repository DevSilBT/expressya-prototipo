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
