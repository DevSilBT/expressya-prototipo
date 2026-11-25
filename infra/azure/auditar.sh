#!/usr/bin/env bash
# Auditoría de SOLO LECTURA del ambiente de prueba de ExpressYa en Azure. En Cloud Shell:
#   bash infra/azure/auditar.sh            # auditoría completa (≈ 1-2 min)
#   bash infra/azure/auditar.sh --consumo  # además corre consumo.sh 24 (proyección de la cuota gratuita)
# No crea, cambia ni borra recursos; no imprime secretos (solo nombres de secretos) y no consume inventario.
# Guarda el informe en auditoria_azure_<fecha>.txt y termina con la lista de AVISOS.
set -uo pipefail
cd "$(dirname "$0")"
source ./comun.sh
MODO="${1:-}"
SALIDA="auditoria_azure_$(date -u +%Y%m%dT%H%M%SZ).txt"
AVISOS=()
aviso() { AVISOS+=("$1"); echo "  ⚠ $1"; }
titulo() { printf '\n== %s\n' "$1"; }
q() { az "$@" 2>/dev/null; }                                # una consulta que falla no detiene la auditoría

{
echo "ExpressYa · auditoría de solo lectura · grupo $RG · $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "Suscripción: $(q account show --query name -o tsv) · usuario: $(q account show --query user.name -o tsv | sed 's/@.*/@…/')"

# ------------------------------------------------------------------ 1. Inventario de recursos
titulo "1. Recursos del grupo (se esperan: 1 PostgreSQL, 1 ambiente, 2 apps, 3 jobs, Log Analytics)"
q resource list -g "$RG" --query "[].{nombre:name, tipo:type, region:location}" -o table
n_pg=$(q postgres flexible-server list -g "$RG" --query "length(@)" -o tsv); n_pg=${n_pg:-0}
[ "$n_pg" = "1" ] || aviso "Hay $n_pg servidores PostgreSQL en el grupo (debe haber exactamente 1: solo uno es gratuito)"
PG=$(q postgres flexible-server list -g "$RG" --query "[0].name" -o tsv)

# ------------------------------------------------------------------ 2. Versión desplegada
titulo "2. Imágenes desplegadas (todas deben llevar el MISMO SHA completo, nunca latest)"
declare -A IMG
for a in core pagos; do IMG[$a]=$(q containerapp show -n "$a" -g "$RG" --query "properties.template.containers[0].image" -o tsv); done
for j in core-worker pagos-worker migrar; do IMG[$j]=$(q containerapp job show -n "$j" -g "$RG" --query "properties.template.containers[0].image" -o tsv); done
for k in core pagos core-worker pagos-worker migrar; do printf '  %-13s %s\n' "$k" "${IMG[$k]:-(no encontrado)}"; done
tags=$(for k in "${!IMG[@]}"; do echo "${IMG[$k]##*:}"; done | sort -u)
[ "$(echo "$tags" | grep -c .)" = "1" ] || aviso "Las piezas no comparten la misma etiqueta: $(echo $tags)"
echo "$tags" | grep -qx latest && aviso "Hay imágenes con la etiqueta latest (usar el SHA completo)"
echo "$tags" | grep -Eqx '[0-9a-f]{40}' || aviso "La etiqueta no es un SHA completo de 40 caracteres"
echo "  Revisiones activas de core:"
q containerapp revision list -n core -g "$RG" --query "[?properties.active].{revision:name, creada:properties.createdTime, trafico:properties.trafficWeight}" -o table

# ------------------------------------------------------------------ 3. Escalado y recursos (costo y arranques en frío)
titulo "3. Escalado y recursos"
for a in core pagos; do
  read -r mn mx cpu mem ing <<<"$(q containerapp show -n "$a" -g "$RG" --query "[properties.template.scale.minReplicas, properties.template.scale.maxReplicas, properties.template.containers[0].resources.cpu, properties.template.containers[0].resources.memory, properties.configuration.ingress.external]" -o tsv | tr '\n' ' ')"
  printf '  %-6s réplicas %s-%s · %s vCPU · %s · ingreso externo=%s\n' "$a" "${mn:-?}" "${mx:-?}" "${cpu:-?}" "${mem:-?}" "${ing:-?}"
  [ "${mn:-0}" = "0" ] || aviso "$a tiene minReplicas=$mn: consume cuota continuamente (regresar a 0 después de la demo)"
done
[ "$(q containerapp show -n pagos -g "$RG" --query properties.configuration.ingress.external -o tsv)" = "false" ] \
  || aviso "pagos tiene ingreso externo: debe ser solo interno (ADR-17)"
for j in core-worker pagos-worker; do
  printf '  %-13s cron %s · timeout %s s\n' "$j" \
    "$(q containerapp job show -n "$j" -g "$RG" --query properties.configuration.scheduleTriggerConfig.cronExpression -o tsv)" \
    "$(q containerapp job show -n "$j" -g "$RG" --query properties.configuration.replicaTimeout -o tsv)"
done

# ------------------------------------------------------------------ 4. Configuración sensible (sin valores de secretos)
titulo "4. Configuración sensible (solo nombres de secretos, nunca valores)"
amb=$(q containerapp show -n core -g "$RG" --query "properties.template.containers[0].env[?name=='AMBIENTE'].value | [0]" -o tsv)
echo "  AMBIENTE de core: ${amb:-(no definido)}"
case "${amb,,}" in produccion|producción|prod) echo "  Sesión simulada: deshabilitada (404)";;
  *) echo "  Sesión simulada: HABILITADA (correcto en prueba; nunca en producción). Comparte la URL solo con el equipo.";; esac
for a in core pagos; do echo "  Secretos de $a: $(q containerapp secret list -n "$a" -g "$RG" --query "[].name" -o tsv | tr '\n' ' ')"; done
for a in core pagos; do
  planos=$(q containerapp show -n "$a" -g "$RG" --query "properties.template.containers[0].env[?value && (contains(name,'SECRETO') || contains(name,'TOKEN') || contains(name,'PASSWORD') || contains(name,'DATABASE_URL'))].name" -o tsv)
  [ -z "$planos" ] || aviso "$a tiene variables sensibles en texto plano (deben ser secretref): $planos"
done
if [ -n "$PG" ]; then
  echo "  PostgreSQL $PG: $(q postgres flexible-server show -g "$RG" -n "$PG" --query "[sku.name, version, highAvailability.mode, backup.backupRetentionDays, network.publicNetworkAccess]" -o tsv | tr '\n' ' ')"
  echo "  Reglas de firewall:"; q postgres flexible-server firewall-rule list -g "$RG" -n "$PG" --query "[].{regla:name, desde:startIpAddress, hasta:endIpAddress}" -o table
  sku=$(q postgres flexible-server show -g "$RG" -n "$PG" --query sku.name -o tsv)
  [ "$sku" = "Standard_B1ms" ] || aviso "La base no es Standard_B1ms ($sku): revisar costo"
  q postgres flexible-server firewall-rule list -g "$RG" -n "$PG" --query "[?startIpAddress=='0.0.0.0' && endIpAddress=='255.255.255.255'].name" -o tsv | grep -q . \
    && aviso "Firewall de PostgreSQL abierto a todo internet (0.0.0.0-255.255.255.255)"
fi

# ------------------------------------------------------------------ 5. Jobs: ejecuciones, fallas y duración (últimas 24 h)
titulo "5. Jobs en las últimas 24 h"
for j in core-worker pagos-worker migrar; do
  q containerapp job execution list -n "$j" -g "$RG" --query "[].[properties.status, properties.startTime, properties.endTime]" -o tsv | J="$j" python3 -c '
import os, sys
from datetime import datetime, timedelta, timezone
f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
j = os.environ["J"]
desde = datetime.now(timezone.utc) - timedelta(hours=24)
n = fallas = 0; seg = []
for l in sys.stdin:
    p = l.split()
    if len(p) < 3 or p[2] in ("", "None") or f(p[1]) < desde: continue
    n += 1; fallas += p[0] != "Succeeded"; seg.append((f(p[2]) - f(p[1])).total_seconds())
prom = sum(seg) / len(seg) if seg else 0
print(f"  {j:<13} ejecuciones {n:>4} · fallidas {fallas:>3} · duración promedio {prom:5.1f} s · máx {max(seg) if seg else 0:5.1f} s")
if fallas: print(f"  ⚠ AVISO-JOB {j}: {fallas} ejecuciones fallidas en 24 h")'
done | tee /tmp/ey_jobs.txt
grep -q "AVISO-JOB" /tmp/ey_jobs.txt && aviso "Hay ejecuciones de jobs fallidas (ver sección 5 y los logs del job)"

# ------------------------------------------------------------------ 6. Base de datos: conexiones, CPU y créditos (24 h)
titulo "6. Métricas de PostgreSQL (24 h; B1ms admite 35 conexiones de usuario)"
if [ -n "$PG" ]; then
  PGID=$(q postgres flexible-server show -g "$RG" -n "$PG" --query id -o tsv)
  met() { q monitor metrics list --resource "$PGID" --metric "$1" --aggregation "$2" --interval PT1H --offset 24h \
            --query "value[0].timeseries[0].data[].$3" -o tsv | python3 -c "import sys; v=[float(x) for x in sys.stdin.read().split() if x not in ('None','')]; print(${4}(v) if v else 'sin datos')"; }
  conn=$(met active_connections Maximum maximum max);  echo "  Conexiones activas (máx. por hora): $conn"
  fail=$(met connections_failed Total total sum);      echo "  Conexiones fallidas (total):        $fail"
  cpu=$(met cpu_percent Maximum maximum max);          echo "  CPU % (máx.):                       $cpu"
  cred=$(met cpu_credits_remaining Minimum minimum min); echo "  Créditos de CPU restantes (mín.):   $cred"
  mapfile -t avs < <(python3 - "$conn" "$fail" "$cred" <<'PY'

import sys
num = lambda s: float(s) if s.replace('.', '', 1).isdigit() else None
c, f, cr = map(num, sys.argv[1:4])
if c is not None and c >= 28: print(f"Conexiones activas altas ({c:.0f} de 35): reducir procesos/pools (Documento Maestro §6.2)")
if f: print(f"Hubo {f:.0f} conexiones fallidas en 24 h: revisar 'too many clients' o credenciales")
if cr is not None and cr < 10: print(f"Créditos de CPU de la base casi agotados ({cr:.0f}): la base quedaría en línea base")
PY
)
  for l in "${avs[@]}"; do aviso "$l"; done
fi

# ------------------------------------------------------------------ 7. Latencia, TLS y disponibilidad (sin consumir inventario)
titulo "7. Latencia desde Cloud Shell (la primera puede incluir arranque en frío)"
FQDN=$(q containerapp show -n core -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
URL="https://$FQDN"; echo "  URL: $URL"
for ruta in /salud /salud /salud /app /app/logo.png "/v1/catalogo?codigo_postal=53400"; do
  curl -s -o /dev/null --max-time 90 -w "  %{http_code}  dns=%{time_namelookup}s tls=%{time_appconnect}s primer_byte=%{time_starttransfer}s total=%{time_total}s  $ruta\n" "$URL$ruta"
done
st=$(curl -s -o /dev/null --max-time 30 -w "%{http_code}" "$URL/app/logo.png")
[ "$st" = "200" ] || aviso "/app/logo.png responde $st: la versión desplegada aún no trae la app del cliente en tiempo real"
fin=$(echo | openssl s_client -connect "$FQDN:443" -servername "$FQDN" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
echo "  Certificado TLS vence: ${fin:-no disponible}"
corr="auditoria-$(date +%s)"
echo "  Rastreo: X-Correlation-Id devuelto = $(curl -s -D - -o /dev/null -H "X-Correlation-Id: $corr" "$URL/salud" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-correlation-id"{print $2}')"

# ------------------------------------------------------------------ 8. Datos de prueba (hallazgo P-13)
titulo "8. Existencias que usan las pruebas automatizadas"
for par in "1 SKU-Y" "3 SKU-Y" "3 SKU-X" "4 SKU-B" "5 SKU-A" "5 SKU-B" "5 SKU-Z"; do
  read -r t sku <<<"$par"
  d=$(curl -s --max-time 30 "$URL/v1/tiendas/$t/disponibilidad?sku=$sku" | python3 -c "import sys,json; s=json.load(sys.stdin)['skus'][0]; print(s['disponible'], s['estado'])" 2>/dev/null)
  echo "  Tienda $t $sku: disponible/estado = ${d:-sin respuesta}"
  case "$t $sku" in "1 SKU-Y"|"3 SKU-Y") [ "${d%% *}" -ge 2 ] 2>/dev/null || aviso "Tienda $t $sku con menos de 2 unidades: probar.sh / probar_c3.sh fallarían sin defecto (P-13)";; esac
done

# ------------------------------------------------------------------ 9. Registros recientes (errores)
titulo "9. Errores recientes en los registros de core (últimas 200 líneas)"
q containerapp logs show -n core -g "$RG" --type console --tail 200 --format text | grep -Ei '"nivel":"(ERROR|WARNING)"|Traceback|too many clients' | tail -15 \
  || echo "  (sin errores en las últimas líneas o core está en cero réplicas)"

if [ "$MODO" = "--consumo" ]; then titulo "10. Consumo frente a la cuota gratuita"; bash ./consumo.sh 24; fi

titulo "RESUMEN"
if [ ${#AVISOS[@]} -eq 0 ]; then echo "  Sin avisos."; else printf '  ⚠ %s\n' "${AVISOS[@]}"; fi
echo "  (${#AVISOS[@]} avisos)"
} 2>&1 | tee "$SALIDA"
echo "Informe guardado en infra/azure/$SALIDA (descárgalo con 'Administrar archivos' de Cloud Shell). No contiene secretos."
