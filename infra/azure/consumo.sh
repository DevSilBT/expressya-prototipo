#!/usr/bin/env bash
# Consumo del ambiente de prueba comparado con la cuota gratuita mensual de Container Apps.
#   bash infra/azure/consumo.sh [horas]      (por defecto, últimas 24 h)
# Solicitudes y CPU salen de Azure Monitor (tardan ~3-5 min en aparecer); las ejecuciones de jobs, de su historial.
set -euo pipefail
cd "$(dirname "$0")"
source ./comun.sh
H="${1:-24}"
VCPU=0.25; MEM=0.5        # recursos asignados por réplica en crear.sh
datos=$(mktemp)
for a in core pagos; do
  id=$(az containerapp show -n "$a" -g "$RG" --query id -o tsv)
  sol=$(az monitor metrics list --resource "$id" --metric Requests --aggregation Total --interval PT1H \
          --offset "${H}h" --query "sum(value[0].timeseries[].data[].total)" -o tsv 2>/dev/null || echo 0)
  rep=$(az monitor metrics list --resource "$id" --metric Replicas --aggregation Maximum --interval PT1M \
          --offset "${H}h" --query "length(value[0].timeseries[].data[?maximum > \`0\`][])" -o tsv 2>/dev/null || echo 0)
  echo "app $a ${sol:-0} ${rep:-0}" >> "$datos"
done
for j in core-worker pagos-worker migrar; do
  az containerapp job execution list -n "$j" -g "$RG" \
     --query "[].[properties.status, properties.startTime, properties.endTime]" -o tsv 2>/dev/null \
     | sed "s/^/job $j /" >> "$datos" || true
done
H="$H" VCPU="$VCPU" MEM="$MEM" python3 - "$datos" <<'PY'
import os, sys
from datetime import datetime, timedelta, timezone
H, VCPU, MEM = float(os.environ["H"]), float(os.environ["VCPU"]), float(os.environ["MEM"])
desde = datetime.now(timezone.utc) - timedelta(hours=H)
f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
apps, jobs = {}, {}
for linea in open(sys.argv[1]):
    p = linea.split()
    if p[0] == "app":
        apps[p[1]] = (float(p[2] or 0), int(float(p[3] or 0)))
    elif len(p) >= 5 and p[4] not in ("", "None"):
        ini, fin = f(p[3]), f(p[4])
        if ini >= desde:
            n, seg, fallas = jobs.get(p[1], (0, 0.0, 0))
            jobs[p[1]] = (n + 1, seg + (fin - ini).total_seconds(), fallas + (p[2] != "Succeeded"))
print(f"\nConsumo de las últimas {H:g} h (grupo expressya-prueba)\n" + "-" * 78)
vcpu_s = 0.0
print(f"{'App':<14}{'Solicitudes':>13}{'Minutos encendida':>20}{'vCPU-s aprox.':>16}")
total_sol = 0
for a, (sol, minutos) in apps.items():
    v = minutos * 60 * VCPU; vcpu_s += v; total_sol += sol
    print(f"{a:<14}{sol:>13,.0f}{minutos:>20}{v:>16,.0f}")
print(f"\n{'Job':<14}{'Ejecuciones':>13}{'Fallidas':>10}{'Seg. promedio':>15}{'vCPU-s':>12}")
for j, (n, seg, fallas) in sorted(jobs.items()):
    v = seg * VCPU; vcpu_s += v
    print(f"{j:<14}{n:>13}{fallas:>10}{seg / n:>15.1f}{v:>12,.0f}")
mes = 30 * 24 / H
print("-" * 78)
print("Proyección a 30 días frente a la cuota gratuita mensual de Container Apps:")
print(f"  vCPU-segundos : {vcpu_s * mes:>12,.0f} de 180,000  ({vcpu_s * mes / 1800:.0f} %)")
print(f"  GiB-segundos  : {vcpu_s * MEM / VCPU * mes:>12,.0f} de 360,000  ({vcpu_s * MEM / VCPU * mes / 3600:.0f} %)")
print(f"  Solicitudes   : {total_sol * mes:>12,.0f} de 2,000,000 ({total_sol * mes / 20000:.2f} %)")
print("La base de datos (B1ms, 750 h/mes) va aparte: está dentro de los servicios gratuitos de 12 meses.")
print("Es una estimación con el tiempo encendido; el costo real se confirma en Administración de costos (8-24 h de retraso).")
PY
rm -f "$datos"
