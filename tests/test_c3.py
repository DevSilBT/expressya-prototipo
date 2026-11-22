"""Construcción C3 · surtido, faltante validado por supervisor, cancelación parcial con reembolso único, conteo físico con
ajuste autorizado e historial (AT-07, AT-08, AT-09). Todo por la API real (FastAPI → funciones SQL → PostgreSQL 16)."""
import uuid

import jwt
from fastapi.testclient import TestClient

from app import worker
from app.db import transaccion
from app.main import app

cliente = TestClient(app)


def token(rol: str, sub: str | None = None) -> dict:
    t = jwt.encode({"sub": sub or str(uuid.uuid4()), "roles": [rol]}, "prueba", algorithm="HS256")
    return {"Authorization": f"Bearer {t}"}


def clave(prefijo="K") -> dict:
    return {"Idempotency-Key": f"{prefijo}-{uuid.uuid4()}"}


def _disponibilidad(tienda: int, sku: str) -> dict:
    return cliente.get(f"/v1/tiendas/{tienda}/disponibilidad", params={"sku": [sku]}).json()["skus"][0]


def _pedido_pagado(cli: dict) -> dict:
    """Cinco líneas en la tienda 3 (CP 53200): A×1, B×1, X×1, Y×2, Z×1."""
    lineas = [{"sku": "SKU-A", "cantidad": 1}, {"sku": "SKU-B", "cantidad": 1}, {"sku": "SKU-X", "cantidad": 1},
              {"sku": "SKU-Y", "cantidad": 2}, {"sku": "SKU-Z", "cantidad": 1}]
    r = cliente.post("/v1/pedidos", headers={**cli, **clave("K1")},
                     json={"codigo_postal": "53200", "lineas": lineas, "envio": 35, "propina": 10})
    assert r.status_code == 201 and r.json()["tienda_id"] == 3, r.text
    pid = r.json()["pedido_id"]
    assert cliente.post(f"/v1/pedidos/{pid}/pago", headers={**cli, **clave("K2")}).json()["estado"] == "PAGADO"
    return cliente.get(f"/v1/pedidos/{pid}", headers=cli).json()


def test_c3_cinco_lineas_una_faltante_cancelacion_parcial_y_reembolso_unico(pagos_falso, monkeypatch):
    """AT-08 + AT-09: las otras cuatro líneas continúan; la faltante se cancela y se reembolsa una sola vez."""
    cli, picker, supervisor = token("CLIENTE"), token("PICKER"), token("SUPERVISOR")
    p = _pedido_pagado(cli)
    pid, lineas = p["pedido_id"], {l["sku"]: l["linea_id"] for l in p["lineas"]}

    assert cliente.post(f"/v1/pedidos/{pid}/preparacion", headers=picker).json()["resultado"] == "EN_PREPARACION"
    assert cliente.post(f"/v1/lineas/{lineas['SKU-A']}/surtido", headers={**cli, **clave()}, json={"cantidad": 1}).status_code == 403
    for sku in ("SKU-A", "SKU-B", "SKU-X", "SKU-Z"):
        r = cliente.post(f"/v1/lineas/{lineas[sku]}/surtido", headers={**picker, **clave()}, json={"cantidad": 1})
        assert r.status_code == 200 and r.json()["resultado"] == "SURTIDA", r.text

    # Y: se pidieron 2, el picker encuentra 1 → reporta 1 faltante; la venta de Y queda bloqueada mientras se valida
    f = cliente.post(f"/v1/lineas/{lineas['SKU-Y']}/faltantes", headers={**picker, **clave("K3")},
                     json={"encontradas": 1, "faltantes": 1, "motivo": "NO_ENCONTRADO"})
    assert f.status_code == 201 and f.json()["resultado"] == "POR_VALIDAR"
    fid = f.json()["faltante_id"]
    y = _disponibilidad(3, "SKU-Y")
    assert y["estado"] == "EN_VERIFICACION" and y["disponible"] == 0
    assert cliente.post(f"/v1/lineas/{lineas['SKU-Y']}/surtido", headers={**picker, **clave()}, json={"cantidad": 1}).json()["estado_pedido"] == "EN_PREPARACION"

    # INV-19: el picker no puede validar su propio faltante (ni por rol, ni aunque tuviera el rol de supervisor)
    assert cliente.post(f"/v1/faltantes/{fid}/validacion", headers={**picker, **clave()}, json={"valido": True, "evidencia": "yo"}).status_code == 403
    k = clave("KV")
    v1 = cliente.post(f"/v1/faltantes/{fid}/validacion", headers={**supervisor, **k}, json={"valido": True, "evidencia": "Revisé anaquel y bodega"})
    v2 = cliente.post(f"/v1/faltantes/{fid}/validacion", headers={**supervisor, **k}, json={"valido": True, "evidencia": "Revisé anaquel y bodega"})
    v3 = cliente.post(f"/v1/faltantes/{fid}/validacion", headers={**supervisor, **clave()}, json={"valido": True, "evidencia": "otra vez"})
    assert v1.json()["resultado"] == "VALIDADO" and v1.json()["estado_pedido"] == "PARCIAL" and v1.json()["reembolso_solicitado"] == "40.00"
    assert v2.json()["repetida"] is True and v3.status_code == 409 and v3.json()["code"] == "FALTANTE_YA_RESUELTO"

    # El worker entrega la solicitud de ajuste a Pagos una sola vez, aunque corra dos veces
    llamadas = []
    monkeypatch.setattr(worker, "cliente_pagos", type("P", (), {"solicitar_ajuste": lambda self, d, k, c: llamadas.append((d, k))})())
    worker.entregar_outbox()
    worker.entregar_outbox()
    mias = [(d, k) for d, k in llamadas if d["pedido_id"] == pid]
    assert mias == [({"pedido_id": pid, "componente": "MERCANCIA", "tipo": "PARCIAL", "monto": "40.00", "motivo": "FALTANTE_PICKING"},
                     f"{fid}#K4")]

    final = cliente.get(f"/v1/pedidos/{pid}/historial", headers=cli).json()
    estados = {l["sku"]: (l["cantidad_surtida"], l["cantidad_cancelada"], l["estado"]) for l in final["lineas"]}
    assert final["estado"] == "PARCIAL" and estados["SKU-Y"] == (1, 1, "SURTIDA_PARCIAL")
    assert sum(1 for s in estados.values() if s[2] == "SURTIDA") == 4
    assert len(final["cancelaciones"]) == 1 and final["cancelaciones"][0]["causa"] == "FALTANTE_PICKING"
    assert final["faltantes"][0]["estado"] == "VALIDADO"
    assert {"SURTIDO", "FALTANTE", "COMPROMISO"} <= {m["tipo"] for m in final["movimientos"]}
    assert {"FaltanteReportado", "FaltanteValidado", "PedidoPreparado", "SolicitudAjusteCobro"} <= {e["tipo"] for e in final["eventos"]}
    with transaccion() as conn:
        n = conn.execute("SELECT count(*) AS n FROM notificaciones.notificacion WHERE tipo = 'CANCELACION_PARCIAL' AND datos->>'pedido_id' = %s",
                         (pid,)).fetchone()["n"]
    assert n == 2                                                   # aplicación y correo, una vez cada uno
    assert _disponibilidad(3, "SKU-Y")["estado"] == "NORMAL"        # la discrepancia quedó resuelta con el movimiento FALTANTE


def test_c3_logica_10_fisica_8_bloqueo_y_ajuste_autorizado():
    """AT-07: la diferencia queda registrada, la disponibilidad incierta se bloquea y solo otra persona autoriza el ajuste."""
    encargado, supervisor, auditor = token("ENCARGADO"), token("SUPERVISOR"), token("AUDITOR")
    with transaccion() as conn:      # tienda 4, SKU-B: 10 unidades lógicas cargadas por 006_datos_c3.sql
        logico = conn.execute("SELECT vendible FROM inventario.existencia WHERE tienda_id = 4 AND sku = 'SKU-B'").fetchone()["vendible"]
    assert logico == 10

    c = cliente.post("/v1/tiendas/4/existencias/SKU-B/conteos", headers={**encargado, **clave()}, json={"cantidad_fisica": 8})
    assert c.status_code == 201 and c.json()["resultado"] == "DISCREPANCIA" and c.json()["diferencia"] == -2
    b = _disponibilidad(4, "SKU-B")
    assert b["estado"] == "EN_VERIFICACION" and b["disponible"] == 0
    aj = c.json()["ajuste_id"]

    mismo = cliente.post(f"/v1/ajustes-inventario/{aj}/autorizacion", headers={**encargado, **clave()},
                         json={"aprobar": True, "evidencia": "lo conté yo"})
    assert mismo.status_code == 403 and mismo.json()["code"] == "AUTORIZADOR_IGUAL_A_SOLICITANTE"
    ok = cliente.post(f"/v1/ajustes-inventario/{aj}/autorizacion", headers={**supervisor, **clave()},
                      json={"aprobar": True, "evidencia": "Reconteo con fotografía del anaquel"})
    assert ok.json()["resultado"] == "APLICADO" and ok.json()["vendible"] == 8 and ok.json()["estado_existencia"] == "NORMAL"

    h = cliente.get("/v1/tiendas/4/existencias/SKU-B/historial", headers=auditor).json()
    assert all(r["coincide"] for r in h["reconstruccion"])
    assert h["discrepancias"][0]["origen"] == "CONTEO" and h["discrepancias"][0]["diferencia"] == -2 and h["discrepancias"][0]["estado"] == "RESUELTA"
    assert h["ajustes"][0]["estado"] == "APLICADO" and h["ajustes"][0]["solicitante_id"] != h["ajustes"][0]["autorizador_id"]
    ultimo = h["movimientos"][0]
    assert (ultimo["tipo"], ultimo["delta"], ultimo["saldo_antes"], ultimo["saldo_despues"]) == ("AJUSTE_NEG", -2, 10, 8)
    assert cliente.get("/v1/tiendas/4/existencias/SKU-B/historial", headers=token("CLIENTE")).status_code == 403


def test_c3_faltante_no_validado_sigue_bloqueado_hasta_el_conteo(pagos_falso):
    """Si el supervisor no confirma el faltante, la línea vuelve a surtirse y el SKU sigue bloqueado hasta el conteo dirigido."""
    cli, picker, supervisor, encargado = token("CLIENTE"), token("PICKER"), token("SUPERVISOR"), token("ENCARGADO")
    p = _pedido_pagado(cli)
    pid, lineas = p["pedido_id"], {l["sku"]: l["linea_id"] for l in p["lineas"]}
    cliente.post(f"/v1/pedidos/{pid}/preparacion", headers=picker)
    fid = cliente.post(f"/v1/lineas/{lineas['SKU-Z']}/faltantes", headers={**picker, **clave()},
                       json={"encontradas": 0, "faltantes": 1, "motivo": "UBICACION_INCORRECTA"}).json()["faltante_id"]
    r = cliente.post(f"/v1/faltantes/{fid}/validacion", headers={**supervisor, **clave()},
                     json={"valido": False, "evidencia": "Estaba en el pasillo 4"})
    assert r.json()["resultado"] == "NO_VALIDADO" and _disponibilidad(3, "SKU-Z")["estado"] == "EN_VERIFICACION"
    vendible = cliente.get("/v1/tiendas/3/existencias/SKU-Z/historial", headers=supervisor).json()["existencia"]["vendible"]
    c = cliente.post("/v1/tiendas/3/existencias/SKU-Z/conteos", headers={**encargado, **clave()}, json={"cantidad_fisica": vendible})
    assert c.json()["resultado"] == "SIN_DIFERENCIA" and c.json()["estado_existencia"] == "NORMAL"
    for sku, linea in lineas.items():
        q = 2 if sku == "SKU-Y" else 1
        r = cliente.post(f"/v1/lineas/{linea}/surtido", headers={**picker, **clave()}, json={"cantidad": q})
        assert r.status_code == 200, r.text
    assert r.json()["estado_pedido"] == "COMPLETO"
