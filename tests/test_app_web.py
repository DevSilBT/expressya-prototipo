"""Interfaz de demostración: registro simulado, catálogo M1 por código postal, bandejas de la tienda y 'mis pedidos'.
Recorre el flujo que hace la pantalla /app usando solo la API pública."""
import uuid

from fastapi.testclient import TestClient

from app.config import ajustes
from app.main import app

cliente = TestClient(app)


def clave(prefijo="K") -> dict:
    return {"Idempotency-Key": f"{prefijo}-{uuid.uuid4()}"}


def sesion(rol: str, nombre: str, **extra) -> dict:
    r = cliente.post("/v1/demo/sesion", json={"rol": rol, "nombre": nombre, **extra})
    assert r.status_code == 200, r.text
    return {"Authorization": r.json()["token"]}


def test_app_web_entrega_html():
    r = cliente.get("/app")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "ExpressYA" in r.text and "/v1/catalogo" in r.text
    assert cliente.get("/", follow_redirects=False).status_code in (302, 307)


def test_registro_simulado_y_bloqueo_en_produccion(monkeypatch):
    r = cliente.post("/v1/demo/sesion", json={"nombre": "Ana López", "correo": "ana@correo.com"})
    assert r.status_code == 200 and r.json()["rol"] == "CLIENTE" and r.json()["token"].startswith("Bearer ")
    misma = cliente.post("/v1/demo/sesion", json={"nombre": "Ana", "usuario_id": r.json()["usuario_id"]}).json()
    assert misma["usuario_id"] == r.json()["usuario_id"]
    assert cliente.post("/v1/demo/sesion", json={"nombre": "Ana", "rol": "ROOT"}).status_code == 422
    monkeypatch.setattr(ajustes, "ambiente", "produccion")
    assert cliente.post("/v1/demo/sesion", json={"nombre": "Ana"}).status_code == 404


def test_catalogo_m1_por_codigo_postal():
    tiendas = cliente.get("/v1/tiendas").json()["tiendas"]
    assert {t["tienda_id"] for t in tiendas} >= {1, 2, 3, 4, 5}
    c = cliente.get("/v1/catalogo", params={"codigo_postal": "53200"}).json()
    assert [t["tienda_id"] for t in c["tiendas"]] == [3]
    a = next(p for p in c["productos"] if p["sku"] == "SKU-A")
    assert a["tienda_id"] == 3 and a["disponible"] > 0 and not a["bloqueado"] and float(a["precio"]) == 30.0
    assert cliente.get("/v1/catalogo", params={"codigo_postal": "99999"}).status_code == 422      # SIN_COBERTURA
    assert cliente.get("/v1/catalogo", params={"codigo_postal": "12"}).status_code == 422


def test_flujo_de_la_pantalla_cliente_y_tienda(pagos_falso):
    cli = sesion("CLIENTE", "Ana")
    r = cliente.post("/v1/pedidos", headers={**cli, **clave("K1")},
                     json={"codigo_postal": "53400", "lineas": [{"sku": "SKU-A", "cantidad": 2}], "envio": 35, "propina": 10})
    assert r.status_code == 201, r.text
    pid = r.json()["pedido_id"]
    assert r.json()["tienda_id"] == 5
    assert cliente.post(f"/v1/pedidos/{pid}/pago", headers={**cli, **clave("K2")}).json()["estado"] == "PAGADO"

    mios = cliente.get("/v1/clientes/me/pedidos", headers=cli).json()["pedidos"]
    assert [p["pedido_id"] for p in mios] == [pid] and mios[0]["estado"] == "PAGADO"

    picker = sesion("PICKER", "Luis", tienda_id=5)
    bandeja = cliente.get("/v1/tiendas/5/pedidos", headers=picker).json()["pedidos"]
    assert pid in [str(p["pedido_id"]) for p in bandeja]
    assert cliente.get("/v1/tiendas/5/pedidos", headers=cli).status_code == 403                # el cliente no ve la bandeja
    assert cliente.get(f"/v1/tiendas/4/pedidos/{pid}", headers=picker).status_code == 404      # otra tienda
    assert cliente.post(f"/v1/pedidos/{pid}/preparacion", headers=picker).status_code == 200
    det = cliente.get(f"/v1/tiendas/5/pedidos/{pid}", headers=picker).json()
    linea = det["lineas"][0]["linea_id"]
    r = cliente.post(f"/v1/lineas/{linea}/faltantes", headers={**picker, **clave("K3")},
                     json={"encontradas": 1, "faltantes": 1, "motivo": "NO_ENCONTRADO"})
    assert r.status_code == 201, r.text
    assert cliente.get(f"/v1/tiendas/5/pedidos/{pid}", headers=picker).json()["faltantes_por_validar"][0]["faltantes"] == 1

    sup = sesion("SUPERVISOR", "Marta", tienda_id=5)
    falt = cliente.get("/v1/tiendas/5/faltantes", headers=sup).json()["faltantes"]
    assert len(falt) == 1 and str(falt[0]["pedido_id"]) == pid
    v = cliente.post(f"/v1/faltantes/{falt[0]['faltante_id']}/validacion", headers={**sup, **clave("V")},
                     json={"valido": True, "evidencia": "Revisé anaquel y bodega"})
    assert v.status_code == 200, v.text
    assert cliente.get("/v1/tiendas/5/faltantes", headers=sup).json()["faltantes"] == []

    c = cliente.post("/v1/tiendas/5/existencias/SKU-B/conteos", headers={**sup, **clave("C")}, json={"cantidad_fisica": 9})
    assert c.status_code == 201 and c.json()["resultado"] == "DISCREPANCIA", c.text
    ajustes_pend = cliente.get("/v1/tiendas/5/ajustes-inventario", headers=sup).json()["ajustes"]
    assert len(ajustes_pend) == 1 and ajustes_pend[0]["cantidad_logica"] == 10 and ajustes_pend[0]["cantidad_fisica"] == 9
    otro = sesion("SUPERVISOR", "Raúl", tienda_id=5)
    a = cliente.post(f"/v1/ajustes-inventario/{ajustes_pend[0]['ajuste_id']}/autorizacion", headers={**otro, **clave("A")},
                     json={"aprobar": True, "evidencia": "Reconteo verificado"})
    assert a.status_code == 200 and a.json()["vendible"] == 9, a.text
    assert cliente.get("/v1/tiendas/5/ajustes-inventario", headers=sup).json()["ajustes"] == []
