#!/usr/bin/env bash
# Permite que GitHub Actions despliegue en el grupo de recursos SIN guardar contraseñas (OIDC, identidad federada).
#   REPO=usuario/repositorio bash infra/azure/conectar_github.sh
# Al terminar imprime tres identificadores (no son secretos) para guardarlos en GitHub. Si tu directorio de Azure
# no permite registrar aplicaciones, despliega a mano con actualizar.sh desde Cloud Shell.
set -euo pipefail
cd "$(dirname "$0")"
: "${REPO:?Define REPO=usuario/repositorio}"
source ./comun.sh
SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
APP_ID=$(az ad app create --display-name "expressya-github-$RG" --query appId -o tsv)
az ad sp create --id "$APP_ID" -o none 2>/dev/null || true
cat > /tmp/fed.json <<JSON
{"name": "github-azure-prueba",
 "issuer": "https://token.actions.githubusercontent.com",
 "subject": "repo:${REPO}:environment:azure-prueba",
 "audiences": ["api://AzureADTokenExchange"]}
JSON
az ad app federated-credential create --id "$APP_ID" --parameters @/tmp/fed.json -o none
sleep 20   # el principal de servicio tarda en propagarse
az role assignment create --assignee "$APP_ID" --role Contributor \
  --scope "/subscriptions/$SUB/resourceGroups/$RG" -o none
cat <<TXT

En GitHub → Settings → Secrets and variables → Actions:
  Secrets:   AZURE_CLIENT_ID=$APP_ID
             AZURE_TENANT_ID=$TENANT
             AZURE_SUBSCRIPTION_ID=$SUB
  Variables: AZURE_RG=$RG
El permiso se limita al grupo de recursos $RG (rol Contributor) y solo al ambiente "azure-prueba" del repositorio.
TXT
