#!/usr/bin/env bash
# Prueba de humo del ambiente desplegado (no requiere usar /docs). En Cloud Shell:
#   bash infra/azure/probar.sh
# Crea un pedido real, lo paga con la pasarela simulada y verifica inventario, idempotencia y autorización.
# Imprime una tabla OK/FALLA y guarda la evidencia en evidencia_azure_<fecha>.txt
set -euo pipefail
cd "$(dirname "$0")"
source ./comun.sh
URL="https://$(az containerapp show -n core -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
SECRETO=$(az containerapp secret show -n core -g "$RG" --secret-name jwt-secreto --query value -o tsv)
SALIDA="evidencia_azure_$(date -u +%Y%m%dT%H%M%SZ).txt"
SECRETO="$SECRETO" URL="$URL" python3 - <<'PY' | tee "$SALIDA"
import base64, hashlib, hmac, json, os, time, urllib.request, urllib.error, uuid

URL, SECRETO = os.environ["URL"], os.environ["SECRETO"]

def token(rol="CLIENTE", sub=None):
    b = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    c = b(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b(json.dumps({"sub": sub or str(uuid.uuid4()), "roles": [rol], "exp": int(time.time()) + 900}).encode())
    f = b(hmac.new(SECRETO.encode(), f"{c}.{p}".encode(), hashlib.sha256).digest())
    return f"Bearer {c}.{p}.{f}"

def http(metodo, ruta, cuerpo=None, cab=None):
    req = urllib.request.Request(URL + ruta, method=metodo, headers={"Content-Type": "application/json", **(cab or {})},
                                 data=json.dumps(cuerpo).encode() if cuerpo is not None else None)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}"), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.time() - t

res = []
def check(nombre, ok, detalle):
    res.append(ok)
    print(f"{'OK   ' if ok else 'FALLA'} | {nombre:<52} | {detalle}")

def disp():
    s, d, _ = http("GET", "/v1/tiendas/1/disponibilidad?sku=SKU-Y")
    return d["skus"][0]["disponible"] if s == 200 else None

print(f"ExpressYa · prueba de humo en {URL} · {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
print("-" * 110)
s, d, t = http("GET", "/salud")
check("1. Salud (API + base de datos)", s == 200, f"HTTP {s} {d} en {t:.1f} s (incluye arranque en frío)")

d0 = disp()
check("2. Disponibilidad tienda 1, SKU-Y", d0 is not None, f"disponible = {d0}")

cliente = str(uuid.uuid4()); tk = token("CLIENTE", cliente); k1 = f"humo-{uuid.uuid4()}"
cuerpo = {"codigo_postal": "53150", "lineas": [{"sku": "SKU-Y", "cantidad": 2}], "envio": 35, "propina": 10}
s, ped, t = http("POST", "/v1/pedidos", cuerpo, {"Authorization": tk, "Idempotency-Key": k1})
pid = ped.get("pedido_id")
check("3. Checkout: reserva 2 × SKU-Y (300 s)", s == 201 and ped.get("estado") == "RESERVADO",
      f"HTTP {s} estado={ped.get('estado')} tienda={ped.get('tienda_id')} total={ped.get('total')}")

s, ped2, _ = http("POST", "/v1/pedidos", cuerpo, {"Authorization": tk, "Idempotency-Key": k1})
check("4. Idempotencia: misma clave → mismo pedido", ped2.get("pedido_id") == pid, f"HTTP {s} mismo pedido_id={ped2.get('pedido_id') == pid}")

d1 = disp()
check("5. La reserva descuenta 2 unidades", d0 is not None and d1 == d0 - 2, f"{d0} → {d1}")

s, _, _ = http("GET", f"/v1/pedidos/{pid}", cab={"Authorization": token("CLIENTE")})
check("6. Otro cliente no ve el pedido (RNF06)", s == 404, f"HTTP {s}")

s, _, _ = http("GET", f"/v1/pedidos/{pid}")
check("7. Sin token → rechazado", s in (401, 403), f"HTTP {s}")

s, pago, t = http("POST", f"/v1/pedidos/{pid}/pago", None, {"Authorization": tk, "Idempotency-Key": f"pago-{uuid.uuid4()}"})
check("8. Pago (pasarela simulada: aprobar)", s == 202 and pago.get("estado") == "PAGADO",
      f"HTTP {s} estado={pago.get('estado')} en {t:.1f} s")

s, fin, _ = http("GET", f"/v1/pedidos/{pid}", cab={"Authorization": tk})
check("9. Consulta del pedido por su dueño", s == 200 and fin.get("estado") == "PAGADO", f"HTTP {s} estado={fin.get('estado')}")

d2 = disp()
check("10. Tras pagar sigue descontado (comprometido)", d2 == d1, f"disponible = {d2}")

print("-" * 110)
print(f"pedido_id de evidencia: {pid}")
print(f"RESULTADO: {sum(res)}/{len(res)} pruebas OK")
PY
echo "Evidencia guardada en infra/azure/$SALIDA (descárgala con el botón 'Administrar archivos' de Cloud Shell)"
