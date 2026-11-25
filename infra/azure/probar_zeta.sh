#!/usr/bin/env bash
# Caso del profesor en Azure (lógico 3, físico 2) con evidencia, sin secretos (usa la sesión simulada del ambiente de prueba):
#   bash infra/azure/probar_zeta.sh
# Fase 1 por la API; luego corre core-worker y pagos-worker a demanda (dos veces) para confirmar el reembolso; fase 2.
set -euo pipefail
cd "$(dirname "$0")"
source ./comun.sh
URL="https://$(az containerapp show -n core -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
SALIDA="evidencia_zeta_$(date -u +%Y%m%dT%H%M%SZ).txt"
{
  echo "ExpressYa · caso Zeta en $URL · $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  python3 ../demo/caso_zeta.py --url "$URL" fase1
  echo "   … ejecutando core-worker y pagos-worker a demanda (dos veces) para llevar el reembolso a Pagos y de vuelta"
  for i in 1 2; do ejecutar_job core-worker; ejecutar_job pagos-worker; done
  python3 ../demo/caso_zeta.py --url "$URL" fase2
} 2>&1 | grep -v "^WARNING" | tee "$SALIDA"
rm -f .caso_zeta.json
echo "Evidencia guardada en infra/azure/$SALIDA"
