#!/usr/bin/env bash
# Crea el ambiente de PRUEBA de ExpressYa en Azure for Students. Ejecutar UNA vez en Azure Cloud Shell (Bash):
#   GHCR_OWNER=tu-usuario-github bash infra/azure/crear.sh
# Requisito: el workflow "ci" ya publicó las imágenes en ghcr.io (rama main).
#
# Qué crea (ver README, sección "Despliegue de prueba en Azure"):
#   - Azure Database for PostgreSQL Flexible Server 16, Burstable B1ms (incluido 12 meses en Azure for Students)
#   - Container Apps (plan de consumo, escala a cero): core (público) y pagos (interno, sin ruta pública)
#   - Container Apps Jobs: core-worker (cada 2 min), pagos-worker (cada 5 min) y migrar (manual)
# Se puede volver a ejecutar si falló a la mitad: reutiliza lo que ya exista.
# Ningún secreto se imprime: las contraseñas se generan aquí y se guardan como secretos de Container Apps.
set -euo pipefail
cd "$(dirname "$0")"
: "${GHCR_OWNER:?Define GHCR_OWNER con tu usuario u organización de GitHub en minúsculas}"
source ./comun.sh
# Si un intento anterior ya creó el servidor en este grupo, se reutiliza (solo hay UN B1ms gratuito).
PG_NAME="${PG_NAME:-$(az postgres flexible-server list -g "$RG" --query "[0].name" -o tsv 2>/dev/null || true)}"
PG_NAME="${PG_NAME:-expressya-pg-$(openssl rand -hex 3)}"

read -rp  "Usuario de GitHub (para leer las imágenes de ghcr.io): " GHCR_USER
read -rsp "Token de GitHub con permiso read:packages (no se muestra): " GHCR_TOKEN; echo

clave() { echo "Ey$(openssl rand -hex 16)-9"; }      # mayúscula, minúsculas, dígitos y guion: cumple la política de Azure
ADMIN_PW=$(clave); CORE_PW=$(clave); PAGOS_PW=$(clave)
JWT=$(openssl rand -hex 32); TOKEN_SERVICIO=$(openssl rand -hex 32)

echo "== 1/6 Proveedores y grupo de recursos ($RG en $LOC)"
asegurar_extension
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.DBforPostgreSQL; do
  az provider register --namespace "$ns" --wait
done
az group create -n "$RG" -l "$LOC" -o none

echo "== 2/6 PostgreSQL 16 administrado ($PG_NAME, B1ms, respaldos 7 días). Tarda ~5-10 min"
if az postgres flexible-server show -g "$RG" -n "$PG_NAME" -o none 2>/dev/null; then
  echo "   el servidor $PG_NAME ya existe: se reutiliza y se renueva la contraseña del administrador"
  az postgres flexible-server update -g "$RG" -n "$PG_NAME" --admin-password "$ADMIN_PW" -o none
else
az postgres flexible-server create -g "$RG" -n "$PG_NAME" -l "$LOC" \
  --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
  --admin-user eyadmin --admin-password "$ADMIN_PW" \
  --public-access 0.0.0.0 --backup-retention 7 --yes -o none
fi
az postgres flexible-server parameter set -g "$RG" -s "$PG_NAME" -n azure.extensions -v PGCRYPTO -o none
HOST=$(az postgres flexible-server show -g "$RG" -n "$PG_NAME" --query fullyQualifiedDomainName -o tsv)
url() { echo "postgresql://$1:$2@$HOST:5432/$3?sslmode=require"; }

echo "== 3/6 Ambiente de Container Apps"
az containerapp env show -n "$ENV_NAME" -g "$RG" -o none 2>/dev/null || az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOC" -o none
DOM=$(az containerapp env show -n "$ENV_NAME" -g "$RG" --query properties.defaultDomain -o tsv)
CORE_URL="https://core.$DOM"; PAGOS_URL="https://pagos.internal.$DOM"
REG=(--registry-server ghcr.io --registry-username "$GHCR_USER" --registry-password "$GHCR_TOKEN")
RECURSOS=(--cpu 0.25 --memory 0.5Gi)

echo "== 4/6 Migración del esquema (roles de mínimo privilegio, DDL y datos de prueba)"
az containerapp job create -n migrar -g "$RG" --environment "$ENV_NAME" \
  --trigger-type Manual --replica-timeout 600 --replica-retry-limit 0 --parallelism 1 --replica-completion-count 1 \
  --image "$(imagen migrador)" "${REG[@]}" "${RECURSOS[@]}" \
  --secrets "admin-url=$(url eyadmin "$ADMIN_PW" postgres)" "core-db-password=$CORE_PW" "pagos-db-password=$PAGOS_PW" \
  --env-vars ADMIN_URL=secretref:admin-url CORE_DB_PASSWORD=secretref:core-db-password PAGOS_DB_PASSWORD=secretref:pagos-db-password \
  -o none
migrar

echo "== 5/6 Servicios: pagos (interno) y core (público)"
SEC_PAGOS=("database-url=$(url expressya_pagos "$PAGOS_PW" expressya_pagos)" "jwt-secreto=$JWT" "token-servicio=$TOKEN_SERVICIO")
ENV_PAGOS=(DATABASE_URL=secretref:database-url JWT_SECRETO=secretref:jwt-secreto TOKEN_SERVICIO=secretref:token-servicio
           CORE_URL="$CORE_URL" PASARELA_MODO_COBRO=aprobar PASARELA_MODO_REEMBOLSO=aplicar MINUTOS_ALERTA_FINANZAS=30)
SEC_CORE=("database-url=$(url expressya_core "$CORE_PW" expressya)" "jwt-secreto=$JWT" "token-servicio=$TOKEN_SERVICIO")
ENV_CORE=(DATABASE_URL=secretref:database-url JWT_SECRETO=secretref:jwt-secreto TOKEN_SERVICIO=secretref:token-servicio
          PAGOS_URL="$PAGOS_URL" TIMEOUT_PAGOS_S=30 DB_POOL_MAX=5 AMBIENTE=azure-prueba)

az containerapp create -n pagos -g "$RG" --environment "$ENV_NAME" --image "$(imagen pagos)" "${REG[@]}" "${RECURSOS[@]}" \
  --ingress internal --target-port 8001 --min-replicas 0 --max-replicas 1 \
  --secrets "${SEC_PAGOS[@]}" --env-vars "${ENV_PAGOS[@]}" -o none
az containerapp create -n core -g "$RG" --environment "$ENV_NAME" --image "$(imagen core)" "${REG[@]}" "${RECURSOS[@]}" \
  --ingress external --target-port 8000 --min-replicas 0 --max-replicas 2 \
  --secrets "${SEC_CORE[@]}" --env-vars "${ENV_CORE[@]}" -o none

echo "== 6/6 Workers como jobs programados"
az containerapp job create -n core-worker -g "$RG" --environment "$ENV_NAME" \
  --trigger-type Schedule --cron-expression "*/2 * * * *" --replica-timeout 110 --replica-retry-limit 0 \
  --parallelism 1 --replica-completion-count 1 \
  --image "$(imagen core)" "${REG[@]}" "${RECURSOS[@]}" --command python correr_worker.py \
  --secrets "${SEC_CORE[@]}" --env-vars "${ENV_CORE[@]}" -o none
az containerapp job create -n pagos-worker -g "$RG" --environment "$ENV_NAME" \
  --trigger-type Schedule --cron-expression "*/5 * * * *" --replica-timeout 120 --replica-retry-limit 0 \
  --parallelism 1 --replica-completion-count 1 \
  --image "$(imagen pagos)" "${REG[@]}" "${RECURSOS[@]}" --command python correr_worker.py \
  --secrets "${SEC_PAGOS[@]}" --env-vars "${ENV_PAGOS[@]}" -o none

echo
echo "LISTO. API pública:   $CORE_URL/docs"
echo "       Salud:          $CORE_URL/salud"
echo "       Token de prueba: bash infra/azure/token.sh CLIENTE"
echo "       Borrar todo:     bash infra/azure/borrar.sh"
