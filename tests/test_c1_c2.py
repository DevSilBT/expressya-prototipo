"""Construcción C1 y C2 · escenarios de aceptación AT-02, AT-03, AT-05 y AT-10, y expiración por el proceso de barrido.
Todas las pruebas usan la aplicación real (FastAPI → servicio → funciones SQL) contra PostgreSQL 16."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import worker
from app.config import ajustes
from app.db import transaccion
from app.inventario import servicio as inventario
from app.main import app
from app.pedidos import servicio

cliente = TestClient(app)
SERVICIO = {"X-Servicio-Token": ajustes.token_servicio}


def _disponible(tienda: int, sku: str) -> int:
    with transaccion() as conn:
        return conn.execute("SELECT inventario.disponible(%s::smallint, %s) AS d", (tienda, sku)).fetchone()["d"]


def _pedido(token, sku="SKU-Z", cantidad=1):
    r = cliente.post("/v1/pedidos", headers={"Authorization": token, "Idempotency-Key": f"K1-{uuid.uuid4()}"},
                     json={"codigo_postal": "53100", "lineas": [{"sku": sku, "cantidad": cantidad}]})
    assert r.status_code == 201, r.text
    return r.json()


def _vencer(pedido_id: str):
    """Mueve la reserva 301 s al pasado (equivale a esperar 5 min sin dormir la prueba)."""
    with transaccion() as conn:
        conn.execute("""UPDATE inventario.reserva SET creada_en = now() - interval '301 seconds',
                        expira_en = now() - interval '1 second' WHERE pedido_id = %s""", (pedido_id,))
        conn.execute("""UPDATE inventario.reserva_linea SET expira_en = now() - interval '1 second'
                        WHERE reserva_id IN (SELECT reserva_id FROM inventario.reserva WHERE pedido_id = %s)""", (pedido_id,))


def test_at02_reserva_vencida_no_admite_cobro(token_cliente, pagos_falso):
    """AT-02 por la API: tras 300 s la reserva ya no se puede usar y no se llama a Pagos."""
    p = _pedido(token_cliente)
    _vencer(p["pedido_id"])
    r = cliente.post(f"/v1/pedidos/{p['pedido_id']}/pago",
                     headers={"Authorization": token_cliente, "Idempotency-Key": f"K2-{uuid.uuid4()}"})
    assert r.status_code == 409 and r.json()["code"] == "RESERVA_VENCIDA"
    assert pagos_falso.llamadas_cobro == []


def test_c1_barrido_expira_y_libera(token_cliente):
    """C1 · proceso de expiración: el barrido cierra la reserva vencida, la bitácora lo registra y el pedido queda NO_PAGADO."""
    antes = _disponible(2, "SKU-Z")
    p = _pedido(token_cliente)
    assert _disponible(2, "SKU-Z") == antes - 1
    _vencer(p["pedido_id"])
    assert _disponible(2, "SKU-Z") == antes            # vencida = ya no cuenta, aun sin barrido (ADR-04)
    with transaccion() as conn:
        assert inventario.expirar_vencidas(conn) >= 1
    worker.entregar_outbox()
    with transaccion() as conn:
        res = conn.execute("SELECT estado FROM inventario.reserva WHERE pedido_id = %s", (p["pedido_id"],)).fetchone()
        ped = conn.execute("SELECT estado FROM pedidos.pedido WHERE pedido_id = %s", (p["pedido_id"],)).fetchone()
        bit = conn.execute("""SELECT count(*) AS n FROM trazabilidad.evento_bitacora
                              WHERE tipo = 'ReservaExpirada' AND pedido_id = %s""", (p["pedido_id"],)).fetchone()
    assert res["estado"] == "EXPIRADA" and ped["estado"] == "NO_PAGADO" and bit["n"] == 1


def test_at03_pago_rechazado_libera_inventario(token_cliente, pagos_falso):
    """AT-03: rechazo → NO_PAGADO, reserva LIBERADA y las unidades vuelven a estar disponibles."""
    pagos_falso.modo = "RECHAZADO"
    antes = _disponible(2, "SKU-Z")
    p = _pedido(token_cliente, cantidad=2)
    assert _disponible(2, "SKU-Z") == antes - 2
    r = cliente.post(f"/v1/pedidos/{p['pedido_id']}/pago",
                     headers={"Authorization": token_cliente, "Idempotency-Key": f"K2-{uuid.uuid4()}"})
    assert r.status_code == 202 and r.json()["estado"] == "NO_PAGADO"
    with transaccion() as conn:
        res = conn.execute("SELECT estado FROM inventario.reserva WHERE pedido_id = %s", (p["pedido_id"],)).fetchone()
    assert res["estado"] == "LIBERADA" and _disponible(2, "SKU-Z") == antes


def test_at05_callback_duplicado_un_solo_compromiso(token_cliente):
    """AT-05: la misma notificación (N1) entregada dos veces produce un solo compromiso y un solo cambio de estado."""
    p = _pedido(token_cliente, cantidad=3)
    with transaccion() as conn:
        conn.execute("UPDATE pedidos.pedido SET estado = 'PAGO_SOLICITADO' WHERE pedido_id = %s", (p["pedido_id"],))
    evento = {"pedido_id": p["pedido_id"], "pago_id": str(uuid.uuid4()), "estado": "APROBADO",
              "notificacion_id": f"N1-{uuid.uuid4()}"}
    r1 = cliente.post("/internal/v1/eventos-pago", headers=SERVICIO, json=evento)
    r2 = cliente.post("/internal/v1/eventos-pago", headers=SERVICIO, json=evento)
    assert r1.status_code == r2.status_code == 200
    with transaccion() as conn:
        comp = conn.execute("""SELECT count(*) AS n, COALESCE(sum(m.delta), 0) AS q FROM inventario.movimiento m
                               JOIN inventario.reserva r ON m.doc_id = r.reserva_id::text
                               WHERE r.pedido_id = %s AND m.tipo = 'COMPROMISO' AND m.saldo = 'comprometido'""",
                            (p["pedido_id"],)).fetchone()
        ped = conn.execute("SELECT estado FROM pedidos.pedido WHERE pedido_id = %s", (p["pedido_id"],)).fetchone()
    assert ped["estado"] == "PAGADO" and comp["n"] == 1 and comp["q"] == 3
    assert cliente.post("/internal/v1/eventos-pago", json=evento).status_code == 403   # sin token de servicio


def test_at10_caida_antes_de_notificar_el_evento_se_recupera(token_cliente, monkeypatch):
    """AT-10: el cambio y su evento se guardan en la misma transacción. Si el proceso cae antes de notificar,
    el evento sigue en la bandeja de salida y el worker lo entrega una sola vez al reiniciar."""
    p = _pedido(token_cliente)
    with transaccion() as conn:
        pend = conn.execute("""SELECT count(*) AS n FROM comun.outbox WHERE agregado_id = %s
                               AND tipo = 'PedidoCreado' AND entregado_en IS NULL""", (p["pedido_id"],)).fetchone()["n"]
    assert pend == 1                                    # persistido, todavía sin notificar ("caída" aquí)
    worker.entregar_outbox()                            # reinicio del worker
    worker.entregar_outbox()                            # una segunda pasada no duplica
    with transaccion() as conn:
        bit = conn.execute("""SELECT count(*) AS n FROM trazabilidad.evento_bitacora
                              WHERE pedido_id = %s AND tipo = 'PedidoCreado'""", (p["pedido_id"],)).fetchone()["n"]
    assert bit == 1


def test_at10_caida_a_mitad_de_la_transaccion_no_deja_huerfanos(monkeypatch):
    """AT-10 (variante): si el proceso falla después de insertar el pedido y antes de reservar, no queda pedido
    ni evento a medias: la transacción completa se revierte."""
    clave = f"K1-{uuid.uuid4()}"

    def falla(*a, **k):
        raise RuntimeError("caída simulada entre persistencia y reserva")

    monkeypatch.setattr(servicio.inventario, "solicitar_reserva", falla)
    with pytest.raises(RuntimeError):
        servicio.checkout(clave, str(uuid.uuid4()), "53100", [{"sku": "SKU-Z", "cantidad": 1}], 0, 0, 0, str(uuid.uuid4()))
    with transaccion() as conn:
        n = conn.execute("SELECT count(*) AS n FROM pedidos.pedido WHERE clave_checkout = %s", (clave,)).fetchone()["n"]
    assert n == 0
