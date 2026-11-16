#!/usr/bin/env bash
# Despliega una versión nueva (la usa el workflow "ci"; también se puede ejecutar a mano en Cloud Shell):
#   GHCR_OWNER=tu-usuario TAG=<sha o latest> bash infra/azure/actualizar.sh
# Orden: primero la migración del esquema y después las aplicaciones (el esquema solo crece, nunca se borra).
set -euo pipefail
cd "$(dirname "$0")"
: "${GHCR_OWNER:?Define GHCR_OWNER}"
source ./comun.sh
asegurar_extension
az containerapp job update -n migrar -g "$RG" --image "$(imagen migrador)" -o none
migrar
az containerapp update     -n pagos        -g "$RG" --image "$(imagen pagos)" -o none
az containerapp update     -n core         -g "$RG" --image "$(imagen core)"  -o none
az containerapp job update -n core-worker  -g "$RG" --image "$(imagen core)"  -o none
az containerapp job update -n pagos-worker -g "$RG" --image "$(imagen pagos)" -o none
echo "Desplegada la versión $TAG: https://$(az containerapp show -n core -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)/docs"
