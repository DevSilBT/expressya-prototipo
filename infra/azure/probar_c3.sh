#!/usr/bin/env bash
# Prueba de C3 en el ambiente desplegado (sin usar /docs). En Cloud Shell:
#   bash infra/azure/probar_c3.sh
# AT-08 + AT-09: pedido de 5 líneas, 1 faltante validado por el supervisor → PARCIAL, cancelación parcial y UN reembolso
# de la mercancía (core → Pagos → core, ejecutando los workers a demanda). AT-07: lógica 10 / física 8 con ajuste autorizado.
# Guarda la evidencia en evidencia_c3_<fecha>.txt
set -euo pipefail
cd "$(dirname "$0")"
source ./comun.sh
URL="https://$(az containerapp show -n core -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
SECRETO=$(az containerapp secret show -n core -g "$RG" --secret-name jwt-secreto --query value -o tsv)
SALIDA="evidencia_c3_$(date -u +%Y%m%dT%H%M%SZ).txt"
ESTADO=$(mktemp)
fase() { SECRETO="$SECRETO" URL="$URL" ESTADO="$ESTADO" FASE="$1" python3 - <<'PY'
import base64, hashlib, hmac, json, os, time, urllib.error, urllib.parse, urllib.request, uuid

URL, SECRETO, FASE, ESTADO = os.environ["URL"], os.environ["SECRETO"], os.environ["FASE"], os.environ["ESTADO"]
st = json.load(open(ESTADO)) if os.path.getsize(ESTADO) else {"res": [], "subs": {r: str(uuid.uuid4()) for r in
      ("CLIENTE", "PICKER", "SUPERVISOR", "ENCARGADO", "AUDITOR")}}

def tok(rol):
    b = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    c = b(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b(json.dumps({"sub": st["subs"][rol], "roles": [rol], "exp": int(time.time()) + 900}).encode())
    return "Bearer " + c + "." + p + "." + b(hmac.new(SECRETO.encode(), f"{c}.{p}".encode(), hashlib.sha256).digest())

def http(metodo, ruta, rol=None, cuerpo=None, clave=None):
    cab = {"Content-Type": "application/json"}
    if rol: cab["Authorization"] = tok(rol)
    if clave: cab["Idempotency-Key"] = clave
    req = urllib.request.Request(URL + ruta, method=metodo, headers=cab, data=json.dumps(cuerpo).encode() if cuerpo is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def check(nombre, ok, detalle):
    st["res"].append(bool(ok))
    print(f"{'OK   ' if ok else 'FALLA'} | {nombre:<58} | {detalle}")

def disp(t, sku):
    return http("GET", f"/v1/tiendas/{t}/disponibilidad?sku={sku}")[1]["skus"][0]

if FASE == "1":
    print(f"ExpressYa · prueba C3 en {URL} · {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("-" * 118)
    lineas = [{"sku": s, "cantidad": 2 if s == "SKU-Y" else 1} for s in ("SKU-A", "SKU-B", "SKU-X", "SKU-Y", "SKU-Z")]
    s, p = http("POST", "/v1/pedidos", "CLIENTE", {"codigo_postal": "53200", "lineas": lineas, "envio": 35, "propina": 10}, f"c3-{uuid.uuid4()}")
    pid = p.get("pedido_id"); st["pid"] = pid
    s2, pago = http("POST", f"/v1/pedidos/{pid}/pago", "CLIENTE", None, f"c3p-{uuid.uuid4()}")
    check("1. Pedido de 5 líneas en la tienda 3, pagado", s == 201 and pago.get("estado") == "PAGADO",
          f"HTTP {s} tienda={p.get('tienda_id')} total={p.get('total')} · pago {s2} {pago.get('estado')}")
    _, ped = http("GET", f"/v1/pedidos/{pid}", "CLIENTE")
    ls = {l["sku"]: l["linea_id"] for l in ped["lineas"]}
    s, r = http("POST", f"/v1/pedidos/{pid}/preparacion", "PICKER")
    check("2. Preparación iniciada (PAGADO → EN_PREPARACION)", r.get("resultado") == "EN_PREPARACION", f"HTTP {s}")
    oks = [http("POST", f"/v1/lineas/{ls[x]}/surtido", "PICKER", {"cantidad": 1}, f"s-{uuid.uuid4()}")[1].get("resultado") for x in ("SKU-A", "SKU-B", "SKU-X", "SKU-Z")]
    check("3. Surtidas A, B, X y Z (ADR-02: se descuenta al surtir)", oks == ["SURTIDA"] * 4, str(oks))
    s, f = http("POST", f"/v1/lineas/{ls['SKU-Y']}/faltantes", "PICKER", {"encontradas": 1, "faltantes": 1, "motivo": "NO_ENCONTRADO"}, f"k3-{uuid.uuid4()}")
    y = disp(3, "SKU-Y"); st["fid"] = f.get("faltante_id")
    check("4. Faltante de 1 Y reportado; venta de Y bloqueada", s == 201 and y["estado"] == "EN_VERIFICACION" and y["disponible"] == 0,
          f"HTTP {s} {f.get('resultado')} · Y: {y['estado']}, disponible {y['disponible']}")
    s, r = http("POST", f"/v1/lineas/{ls['SKU-Y']}/surtido", "PICKER", {"cantidad": 1}, f"s-{uuid.uuid4()}")
    check("5. Surtida la Y encontrada; el pedido sigue en preparación", r.get("estado_pedido") == "EN_PREPARACION", f"HTTP {s}")
    s, _ = http("POST", f"/v1/faltantes/{st['fid']}/validacion", "PICKER", {"valido": True, "evidencia": "lo valido yo"}, f"v-{uuid.uuid4()}")
    check("6. El picker no puede validar su faltante (INV-19)", s == 403, f"HTTP {s}")
    k = f"v-{uuid.uuid4()}"
    s, v = http("POST", f"/v1/faltantes/{st['fid']}/validacion", "SUPERVISOR", {"valido": True, "evidencia": "Revisé anaquel y bodega"}, k)
    s2, v2 = http("POST", f"/v1/faltantes/{st['fid']}/validacion", "SUPERVISOR", {"valido": True, "evidencia": "Revisé anaquel y bodega"}, k)
    check("7. Supervisor valida → PARCIAL, reembolso de la mercancía", v.get("estado_pedido") == "PARCIAL",
          f"HTTP {s} {v.get('resultado')} reembolso={v.get('reembolso_solicitado')} · reintento repetida={v2.get('repetida')}")
    print("   … ejecutando core-worker y pagos-worker a demanda (dos veces) para llevar el reembolso a Pagos y de vuelta")
elif FASE == "2":
    pid = st["pid"]
    _, h = http("GET", f"/v1/pedidos/{pid}/historial", "AUDITOR")
    lin = {l["sku"]: f"{l['cantidad_surtida']}+{l['cantidad_cancelada']} {l['estado']}" for l in h["lineas"]}
    check("8. Estado final: 4 líneas SURTIDA y Y 1 surtida + 1 cancelada", h["estado"] == "PARCIAL" and lin["SKU-Y"] == "1+1 SURTIDA_PARCIAL"
          and sum(v.endswith(" SURTIDA") for v in lin.values()) == 4, f"{h['estado']} · Y {lin['SKU-Y']}")
    notif = [n["tipo"] for n in h["notificaciones"]]
    check("9. Cliente avisado: cancelación parcial y reembolso confirmado (1 vez por canal)",
          notif.count("CANCELACION_PARCIAL") == 2 and notif.count("REEMBOLSO_CONFIRMADO") == 2, str(sorted(set(notif))))
    check("10. Historial del pedido: quién, qué y por qué", len(h["cancelaciones"]) == 1 and h["faltantes"][0]["estado"] == "VALIDADO"
          and {"SURTIDO", "FALTANTE"} <= {m["tipo"] for m in h["movimientos"]},
          f"cancelaciones={len(h['cancelaciones'])} faltante={h['faltantes'][0]['estado']} movimientos={len(h['movimientos'])} eventos={len(h['eventos'])}")
    _, hx = http("GET", "/v1/tiendas/4/existencias/SKU-B/historial", "AUDITOR")
    log = hx["existencia"]["vendible"]
    s, c = http("POST", "/v1/tiendas/4/existencias/SKU-B/conteos", "ENCARGADO", {"cantidad_fisica": log - 2}, f"c-{uuid.uuid4()}")
    b = disp(4, "SKU-B")
    check(f"11. AT-07: lógica {log}, física {log - 2} → discrepancia y SKU bloqueado", c.get("diferencia") == -2 and b["disponible"] == 0,
          f"HTTP {s} {c.get('resultado')} · B: {b['estado']}, disponible {b['disponible']}")
    s, _ = http("POST", f"/v1/ajustes-inventario/{c['ajuste_id']}/autorizacion", "ENCARGADO", {"aprobar": True, "evidencia": "lo conté yo"}, f"a-{uuid.uuid4()}")
    s2, a = http("POST", f"/v1/ajustes-inventario/{c['ajuste_id']}/autorizacion", "SUPERVISOR", {"aprobar": True, "evidencia": "Reconteo con foto"}, f"a-{uuid.uuid4()}")
    check("12. Ajuste: quien contó no autoriza (403); otra persona sí", s == 403 and a.get("resultado") == "APLICADO",
          f"{s} / {s2} {a.get('resultado')} vendible={a.get('vendible')} estado={a.get('estado_existencia')}")
    _, hx = http("GET", "/v1/tiendas/4/existencias/SKU-B/historial", "AUDITOR")
    check("13. Saldos reconstruidos desde movimientos", all(r["coincide"] for r in hx["reconstruccion"]),
          ", ".join(f"{r['saldo']}={r['desde_movimientos']}/{r['registrado']}" for r in hx["reconstruccion"]))
    print("-" * 118)
    print(f"pedido_id de evidencia: {pid}")
    print(f"RESULTADO: {sum(st['res'])}/{len(st['res'])} pruebas OK")
json.dump(st, open(ESTADO, "w"))
PY
}
{
  fase 1
  for i in 1 2; do ejecutar_job core-worker; ejecutar_job pagos-worker; done
  fase 2
} 2>&1 | grep -v "^WARNING" | tee "$SALIDA"
rm -f "$ESTADO"
echo "Evidencia guardada en infra/azure/$SALIDA"
