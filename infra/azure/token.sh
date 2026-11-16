#!/usr/bin/env bash
# Genera un JWT de prueba (15 min) firmado con el secreto del ambiente, sin mostrar el secreto.
#   bash infra/azure/token.sh CLIENTE [sub]      roles: CLIENTE, ATENCION, SUPERVISOR, FINANZAS, AUDITOR
set -euo pipefail
cd "$(dirname "$0")"
source ./comun.sh
ROL="${1:-CLIENTE}"; SUB="${2:-$(cat /proc/sys/kernel/random/uuid)}"
SECRETO=$(az containerapp secret show -n core -g "$RG" --secret-name jwt-secreto --query value -o tsv)
SECRETO="$SECRETO" python3 - "$ROL" "$SUB" <<'PY'
import base64, hashlib, hmac, json, os, sys, time
b = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
cab = b(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
cuerpo = b(json.dumps({"sub": sys.argv[2], "roles": [sys.argv[1]], "exp": int(time.time()) + 900}).encode())
firma = b(hmac.new(os.environ["SECRETO"].encode(), f"{cab}.{cuerpo}".encode(), hashlib.sha256).digest())
print(f"Bearer {cab}.{cuerpo}.{firma}")
PY
