#!/usr/bin/env bash
# Elimina TODO el ambiente de prueba (base de datos incluida) para que no consuma crédito.
set -euo pipefail
cd "$(dirname "$0")"
source ./comun.sh
read -rp "Se borrará el grupo de recursos '$RG' con su base de datos. Escribe el nombre para confirmar: " r
[ "$r" = "$RG" ] || { echo "Cancelado"; exit 1; }
az group delete -n "$RG" --yes --no-wait
echo "Borrado en curso ($RG)."
