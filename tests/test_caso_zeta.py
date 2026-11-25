"""Caso del profesor (24-nov-2026): inventario lógico 3, físico 2, por la API pública y la sesión simulada.
El cliente pide 3 × Producto Zeta; el picker encuentra 2; el supervisor (otra persona) valida el faltante; el pedido queda
PARCIAL con reembolso de 1 unidad; inventario, estado y bitácora reflejan el resultado (con quién y cuándo)."""
import uuid

from fastapi.testclient import TestClient

from app.main import app

cliente = TestClient(app)


def clave(prefijo="K") -> dict:
    return {"Idempotency-Key": f"{prefijo}-{uuid.uuid4()}"}


def sesion(rol: str, nombre: str, **extra) -> dict:
    r = cliente.post("/v1/demo/sesion", json={"rol": rol, "nombre": nombre, **extra})
    assert r.status_code == 200, r.text
    return {"Authorization": r.json()["token"]}


def zeta() -> dict:
    return cliente.get("/v1/tiendas/5/disponibilidad", params={"sku": "SKU-ZETA"}).json()["skus"][0]


def test_caso_zeta_logico_3_fisico_2(pagos_falso):
    encargado, ana = sesion("ENCARGADO", "Rosa", tienda_id=5), sesion("CLIENTE", "Ana")
    luis, marta = sesion("PICKER", "Luis", tienda_id=5), sesion("SUPERVISOR", "Marta", tienda_id=5)

    esc = cliente.post("/v1/demo/escenario-zeta", headers=encargado)
    assert esc.status_code == 200 and esc.json()["vendible"] == 3, esc.text
    assert zeta()["disponible"] == 3

    # 1-2 · el cliente pide 3 (el sistema las muestra disponibles) y paga
    r = cliente.post("/v1/pedidos", headers={**ana, **clave("K1")},
                     json={"codigo_postal": "53400", "lineas": [{"sku": "SKU-ZETA", "cantidad": 3}], "envio": 35, "propina": 0})
    assert r.status_code == 201 and r.json()["tienda_id"] == 5, r.text
    pid = r.json()["pedido_id"]
    assert cliente.post(f"/v1/pedidos/{pid}/pago", headers={**ana, **clave("K2")}).json()["estado"] == "PAGADO"

    # 3 · el picker ve producto y cantidades exactas (calculadas en el servidor)
    assert cliente.post(f"/v1/pedidos/{pid}/preparacion", headers=luis).status_code == 200
    det = cliente.get(f"/v1/tiendas/5/pedidos/{pid}", headers=luis).json()
    l = det["lineas"][0]
    assert (l["descripcion"], l["cantidad_pedida"], l["cantidad_surtida"], l["por_validar"], l["pendiente"]) == ("Producto Zeta", 3, 0, 0, 3)
    assert det["preparacion"]["picker"] == "Luis"
    bandeja = cliente.get("/v1/tiendas/5/pedidos", headers=luis).json()["pedidos"]
    assert any(str(p["pedido_id"]) == pid and p["unidades_pedidas"] == 3 for p in bandeja)

    # validaciones: no se puede surtir más de lo pendiente ni dejar faltante sin motivo
    url = f"/v1/lineas/{l['linea_id']}/surtido-verificado"
    assert cliente.post(url, headers={**luis, **clave("R")}, json={"encontradas": 4}).json()["code"] == "CANTIDAD_INVALIDA"
    assert cliente.post(url, headers={**luis, **clave("R")}, json={"encontradas": 2}).json()["code"] == "MOTIVO_REQUERIDO"

    # 4 · «encontré 2 de 3»: surte 2 y reporta 1 faltante en una sola operación; el reintento no duplica
    k = clave("R")
    s = cliente.post(url, headers={**luis, **k}, json={"encontradas": 2, "motivo": "NO_ENCONTRADO"})
    assert s.status_code == 200 and s.json()["resultado"] == "SURTIDO_CON_FALTANTE", s.text
    assert (s.json()["surtidas"], s.json()["faltantes"]) == (2, 1)
    assert cliente.post(url, headers={**luis, **k}, json={"encontradas": 2, "motivo": "NO_ENCONTRADO"}).json()["repetida"] is True
    l = cliente.get(f"/v1/tiendas/5/pedidos/{pid}", headers=luis).json()["lineas"][0]
    assert (l["cantidad_surtida"], l["por_validar"], l["pendiente"], l["estado_existencia"]) == (2, 1, 0, "EN_VERIFICACION")
    assert zeta()["disponible"] == 0 and zeta()["estado"] == "EN_VERIFICACION"           # ADR-27: venta bloqueada
    assert cliente.post("/v1/demo/escenario-zeta", headers=encargado).json()["code"] == "ESCENARIO_OCUPADO"

    # 5 · el supervisor (otra persona) valida; el picker no puede validar su propio faltante
    fid = s.json()["faltante_id"]
    ev = {"valido": True, "evidencia": "Revisé anaquel y bodega"}
    assert cliente.post(f"/v1/faltantes/{fid}/validacion", headers={**luis, **clave("V")}, json=ev).status_code == 403
    v = cliente.post(f"/v1/faltantes/{fid}/validacion", headers={**marta, **clave("V")}, json=ev).json()
    assert v["estado_pedido"] == "PARCIAL" and v["reembolso_solicitado"] == "20.00", v

    # 6 · estado, inventario y bitácora reflejan el resultado
    assert zeta()["estado"] == "NORMAL" and zeta()["vendible"] == 0 and zeta()["comprometido"] == 0
    hist = cliente.get("/v1/tiendas/5/existencias/SKU-ZETA/historial", headers=marta).json()
    assert all(x["coincide"] for x in hist["reconstruccion"])
    b = cliente.get(f"/v1/pedidos/{pid}/bitacora", headers=ana).json()
    tipos = [e["tipo"] for e in b["eventos"]]
    assert {"PagoResultado", "LineaSurtida", "FaltanteReportado", "FaltanteValidado", "SolicitudAjusteCobro", "PedidoPreparado"} <= set(tipos)
    assert {e["por"] for e in b["eventos"]} <= {"Tú", "La tienda", "Pagos", "Sistema"}
    assert cliente.get(f"/v1/pedidos/{pid}/bitacora", params={"vista": "personal"}, headers=ana).status_code == 403
    bp = cliente.get(f"/v1/pedidos/{pid}/bitacora", params={"vista": "personal"}, headers=marta).json()
    por = {e["por"] for e in bp["eventos"]}
    assert {"Cliente · Ana", "Picker · Luis", "Supervisor · Marta"} <= por, por

    # el escenario se puede repetir: reposición auditada a 3
    assert cliente.post("/v1/demo/escenario-zeta", headers=encargado).json()["agregadas"] == 3


def test_tiempo_de_servidor_en_cada_respuesta():
    r = cliente.get("/salud")
    assert r.headers["Server-Timing"].startswith("app;dur=") and r.headers["X-Correlation-Id"]


def test_escenario_zeta_requiere_personal_y_no_existe_en_produccion(monkeypatch):
    from app.config import ajustes
    assert cliente.post("/v1/demo/escenario-zeta", headers=sesion("CLIENTE", "Ana")).status_code == 403
    encargado = sesion("ENCARGADO", "Rosa", tienda_id=5)          # token emitido antes de simular producción
    monkeypatch.setattr(ajustes, "ambiente", "produccion")
    assert cliente.post("/v1/demo/escenario-zeta", headers=encargado).status_code == 404
