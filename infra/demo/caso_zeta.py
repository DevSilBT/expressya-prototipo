#!/usr/bin/env python3
"""Caso del profesor de punta a punta por la API pública (solo biblioteca estándar; usa la sesión simulada, sin secretos).
  python3 infra/demo/caso_zeta.py --url https://<core> fase1     # pedido, pago, surtido 2 de 3, faltante y validación
  python3 infra/demo/caso_zeta.py --url https://<core> fase2     # tras correr los jobs: reembolso confirmado y bitácora
Inventario lógico 3, físico 2 → PARCIAL, 1 unidad cancelada y reembolsada, inventario y bitácora actualizados.
El estado entre fases se guarda en .caso_zeta.json. No existe en producción (la sesión simulada responde 404)."""
import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

EST = pathlib.Path(".caso_zeta.json")
res = []


def http(url, metodo="GET", cuerpo=None, token=None, clave=None):
    cab = {"Content-Type": "application/json"}
    if token: cab["Authorization"] = token
    if clave: cab["Idempotency-Key"] = clave
    req = urllib.request.Request(url, method=metodo, headers=cab, data=json.dumps(cuerpo).encode() if cuerpo is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(nombre, ok, detalle=""):
    res.append(bool(ok)); print(f"{'OK   ' if ok else 'FALLA'} | {nombre:<66} | {detalle}")


def fase1(u):
    tok = {}
    for rol, nombre in (("ENCARGADO", "Rosa"), ("CLIENTE", "Ana"), ("PICKER", "Luis"), ("SUPERVISOR", "Marta")):
        s, d = http(u + "/v1/demo/sesion", "POST", {"nombre": nombre, "rol": rol, "tienda_id": None if rol == "CLIENTE" else 5})
        if s != 200: sys.exit(f"La sesión simulada no está disponible (HTTP {s}).")
        tok[rol] = d["token"]
    s, e = http(u + "/v1/demo/escenario-zeta", "POST", token=tok["ENCARGADO"])
    disp = lambda: http(u + "/v1/tiendas/5/disponibilidad?sku=SKU-ZETA")[1]["skus"][0]
    check("1. Escenario: Producto Zeta con inventario lógico 3 en la tienda 5", s == 200 and disp()["disponible"] == 3,
          f"HTTP {s} · repuestas {e.get('agregadas')} · {e.get('detail', '')}")
    s, p = http(u + "/v1/pedidos", "POST", {"codigo_postal": "53400", "lineas": [{"sku": "SKU-ZETA", "cantidad": 3}], "envio": 35},
                tok["CLIENTE"], f"zeta-{uuid.uuid4()}")
    pid = p.get("pedido_id")
    s2, pago = http(f"{u}/v1/pedidos/{pid}/pago", "POST", token=tok["CLIENTE"], clave=f"zeta-p-{uuid.uuid4()}")
    check("2. Cliente pide 3 (el sistema las muestra) y paga", s == 201 and pago.get("estado") == "PAGADO",
          f"HTTP {s} tienda {p.get('tienda_id')} · pago {s2} {pago.get('estado')}")
    http(f"{u}/v1/pedidos/{pid}/preparacion", "POST", token=tok["PICKER"])
    _, det = http(f"{u}/v1/tiendas/5/pedidos/{pid}", token=tok["PICKER"])
    l = det["lineas"][0]
    check("3. Picker ve producto y cantidades: Zeta · pedidas 3 · por surtir 3", (l["descripcion"], l["cantidad_pedida"], l["pendiente"]) == ("Producto Zeta", 3, 3),
          f"{l['descripcion']} · pedidas {l['cantidad_pedida']} · por surtir {l['pendiente']}")
    s, r = http(f"{u}/v1/lineas/{l['linea_id']}/surtido-verificado", "POST", {"encontradas": 2, "motivo": "NO_ENCONTRADO"},
                tok["PICKER"], f"zeta-r-{uuid.uuid4()}")
    z = disp()
    check("4. Picker registra «encontré 2»: surte 2 y reporta 1 faltante", s == 200 and (r.get("surtidas"), r.get("faltantes")) == (2, 1),
          f"HTTP {s} {r.get('resultado')}")
    check("5. SKU en verificación: la venta de Zeta queda bloqueada", z["estado"] == "EN_VERIFICACION" and z["disponible"] == 0, f"{z['estado']} · disponible {z['disponible']}")
    s, _ = http(f"{u}/v1/faltantes/{r['faltante_id']}/validacion", "POST", {"valido": True, "evidencia": "lo valido yo"}, tok["PICKER"], f"v-{uuid.uuid4()}")
    check("6. El picker no puede validar su propio faltante (INV-19)", s == 403, f"HTTP {s}")
    s, v = http(f"{u}/v1/faltantes/{r['faltante_id']}/validacion", "POST", {"valido": True, "evidencia": "Revisé anaquel y bodega"},
                tok["SUPERVISOR"], f"v-{uuid.uuid4()}")
    z = disp()
    check("7. Supervisor valida → PARCIAL, se cancela 1 y se pide reembolso de $20.00",
          v.get("estado_pedido") == "PARCIAL" and v.get("reembolso_solicitado") == "20.00", f"HTTP {s} {v.get('resultado')}")
    check("8. Inventario: lógico 0 = físico 0, comprometido 0, NORMAL", (z["vendible"], z["comprometido"], z["estado"]) == (0, 0, "NORMAL"),
          f"vendible {z['vendible']} · comprometido {z['comprometido']} · {z['estado']}")
    EST.write_text(json.dumps({"pid": pid, "tok": tok}))
    print(f"   pedido_id: {pid}")


def fase2(u):
    st = json.loads(EST.read_text()); pid, tok = st["pid"], st["tok"]
    _, b = http(f"{u}/v1/pedidos/{pid}/bitacora", token=tok["CLIENTE"])
    titulos = [e["titulo"] for e in b["eventos"]]
    check("9. Cliente: PARCIAL y aviso «Reembolso confirmado»", b["estado"] == "PARCIAL" and b["reembolso_confirmado"], b["estado_texto"])
    check("10. Bitácora del cliente: pago, surtido, faltante, cancelación, reembolso", {"Pago aprobado", "Producto listo", "Producto no disponible",
          "Reembolso solicitado", "Reembolso confirmado"} <= set(titulos), f"{len(titulos)} movimientos")
    _, bp = http(f"{u}/v1/pedidos/{pid}/bitacora?vista=personal", token=tok["SUPERVISOR"])
    por = {e["por"] for e in bp["eventos"]}
    check("11. Bitácora del personal: quién y cuándo", {"Cliente · Ana", "Picker · Luis", "Supervisor · Marta"} <= por, ", ".join(sorted(por)))
    _, h = http(u + "/v1/tiendas/5/existencias/SKU-ZETA/historial", token=tok["SUPERVISOR"])
    check("12. Saldos reconstruidos desde movimientos", all(x["coincide"] for x in h["reconstruccion"]),
          ", ".join(f"{x['saldo']} {x['desde_movimientos']}/{x['registrado']}" for x in h["reconstruccion"]))


ap = argparse.ArgumentParser(description=__doc__); ap.add_argument("fase", choices=["fase1", "fase2"]); ap.add_argument("--url", required=True)
a = ap.parse_args()
(fase1 if a.fase == "fase1" else fase2)(a.url.rstrip("/"))
print(f"RESULTADO {a.fase}: {sum(res)}/{len(res)} OK")
sys.exit(0 if all(res) else 1)
