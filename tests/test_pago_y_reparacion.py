"""TP-S3-12/13/14/16 · Pago aprobado, pago tardío (R-D02), timeout sin doble cobro y recuperación de un evento perdido."""
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from app.db import transaccion
from app.main import app
from app.pedidos import servicio

cliente = TestClient(app)


def _nuevo_pedido(token, sku="SKU-Y"):
    r = cliente.post("/v1/pedidos", headers={"Authorization": token, "Idempotency-Key": f"K1-{uuid.uuid4()}"},
                     json={"codigo_postal": "53100", "lineas": [{"sku": sku, "cantidad": 1}], "envio": "20.00"})
    assert r.status_code == 201, r.text
    return r.json()["pedido_id"]


def test_pago_aprobado_confirma_y_compromete(token_cliente, pagos_falso):
    pedido = _nuevo_pedido(token_cliente)
    r = cliente.post(f"/v1/pedidos/{pedido}/pago", headers={"Authorization": token_cliente, "Idempotency-Key": f"K2-{uuid.uuid4()}"})
    assert r.status_code == 202 and r.json()["estado"] == "PAGADO"


def test_pago_tardio_no_revive_la_reserva_y_reembolsa_una_vez(token_cliente, pagos_falso):
    pedido = _nuevo_pedido(token_cliente)
    with transaccion() as conn:   # se simula que pasaron 301 s desde la creación de la reserva
        conn.execute("""UPDATE inventario.reserva SET creada_en = now() - interval '301 seconds',
                        expira_en = now() - interval '1 second' WHERE pedido_id = %s""", (pedido,))
        conn.execute("UPDATE inventario.reserva_linea SET expira_en = now() - interval '1 second' "
                     "WHERE reserva_id IN (SELECT reserva_id FROM inventario.reserva WHERE pedido_id = %s)", (pedido,))
    evento = {"pedido_id": pedido, "pago_id": str(uuid.uuid4()), "estado": "APROBADO", "notificacion_id": f"N1-{uuid.uuid4()}"}
    assert servicio.procesar_resultado_pago(evento, None)["desenlace"] == "PAGO_SIN_EXISTENCIA"
    servicio.procesar_resultado_pago(evento, None)                     # la misma N1 llega otra vez
    with transaccion() as conn:
        n = conn.execute("""SELECT count(*) AS n FROM comun.outbox WHERE tipo = 'SolicitudAjusteCobro'
                            AND agregado_id = %s""", (pedido,)).fetchone()["n"]
        comp = conn.execute("""SELECT count(*) AS n FROM inventario.movimiento m JOIN inventario.reserva r ON m.doc_id = r.reserva_id::text
                               WHERE r.pedido_id = %s AND m.tipo = 'COMPROMISO'""", (pedido,)).fetchone()["n"]
    assert n == 1 and comp == 0


def test_timeout_no_reenvia_el_cobro(token_cliente, pagos_falso):
    pagos_falso.modo = "TIMEOUT"
    pedido = _nuevo_pedido(token_cliente)
    h = {"Authorization": token_cliente, "Idempotency-Key": f"K2-{uuid.uuid4()}"}
    assert cliente.post(f"/v1/pedidos/{pedido}/pago", headers=h).json()["estado"] == "PAGO_INCIERTO"
    h2 = {"Authorization": token_cliente, "Idempotency-Key": f"K2-{uuid.uuid4()}"}
    r = cliente.post(f"/v1/pedidos/{pedido}/pago", headers=h2)
    assert r.status_code == 409 and r.json()["code"] == "PAGO_EN_CURSO"
    assert len(pagos_falso.llamadas_cobro) == 1


def test_reparacion_de_evento_de_pago_perdido(token_cliente, pagos_falso):
    pedido = _nuevo_pedido(token_cliente)
    with transaccion() as conn:
        conn.execute("UPDATE pedidos.pedido SET estado = 'PAGO_SOLICITADO' WHERE pedido_id = %s", (pedido,))
    pagos_falso.finales = [{"pedido_id": pedido, "pago_id": str(uuid.uuid4()), "estado": "APROBADO",
                            "notificacion_id": f"N1-{uuid.uuid4()}"}]          # Pagos aprobó, pero el evento nunca llegó
    assert servicio.reparar("2000-01-01") == 1
    with transaccion() as conn:
        estado = conn.execute("SELECT estado FROM pedidos.pedido WHERE pedido_id = %s", (pedido,)).fetchone()["estado"]
        rep = conn.execute("SELECT count(*) AS n FROM comun.outbox WHERE tipo = 'REPARACION_APLICADA' AND agregado_id = %s",
                           (pedido,)).fetchone()["n"]
    assert estado == "PAGADO" and rep == 1
    assert servicio.reparar("2000-01-01") == 0                         # segundo ciclo: nada que reparar


def test_cliente_no_ve_ni_paga_pedidos_ajenos(token_cliente, pagos_falso):
    import jwt
    pedido = _nuevo_pedido(token_cliente)
    otro = "Bearer " + jwt.encode({"sub": str(uuid.uuid4()), "roles": ["CLIENTE"]}, "prueba", algorithm="HS256")
    assert cliente.get(f"/v1/pedidos/{pedido}", headers={"Authorization": otro}).status_code == 404
    r = cliente.post(f"/v1/pedidos/{pedido}/pago", headers={"Authorization": otro, "Idempotency-Key": f"K2-{uuid.uuid4()}"})
    assert r.status_code == 404 and pagos_falso.llamadas_cobro == []
    assert cliente.get(f"/v1/pedidos/{pedido}").status_code == 401
