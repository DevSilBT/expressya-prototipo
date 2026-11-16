"""Construcción C2 · servicio de Pagos con pasarela simulada: aprobado, rechazado, timeout, callback duplicado,
reembolso parcial repetido y resolución manual por Finanzas (AT-03, AT-05, AT-06, AT-09)."""
import uuid

import jwt
from fastapi.testclient import TestClient

from app.config import ajustes
from app.main import app, tx

cliente = TestClient(app)
S = {"X-Servicio-Token": "token-de-prueba"}


def _cobro(pedido=None, monto="125.00", clave=None):
    pedido = pedido or str(uuid.uuid4())
    r = cliente.post("/v1/cobros", headers={**S, "Idempotency-Key": clave or f"K2-{uuid.uuid4()}"},
                     json={"pedido_id": pedido, "monto": monto})
    return pedido, r


def _eventos(agregado: str, tipo: str) -> int:
    with tx() as conn:
        return conn.execute("SELECT count(*) AS n FROM pagos.outbox WHERE agregado_id = %s AND tipo = %s",
                            (agregado, tipo)).fetchone()["n"]


def test_cobro_aprobado_y_reintento_con_la_misma_k2(pasarela):
    clave = f"K2-{uuid.uuid4()}"
    pedido, r1 = _cobro(clave=clave)
    _, r2 = _cobro(pedido, clave=clave)
    assert r1.status_code == 202 and r1.json()["estado"] == "APROBADO"
    assert r2.json()["repetida"] is True and r2.json()["pago_id"] == r1.json()["pago_id"]
    assert pasarela["cobrar"] == 1                                         # el reintento NO vuelve a cobrar
    assert _cobro(pedido, monto="999.00", clave=clave)[1].status_code == 422   # misma clave, otro cuerpo


def test_at03_cobro_rechazado_queda_persistido(pasarela):
    ajustes.modo_cobro = "rechazar"
    _, r = _cobro()
    assert r.json()["estado"] == "RECHAZADO"
    assert _eventos(r.json()["pago_id"], "PagoRechazado") == 1


def test_at06_timeout_queda_incierto_sin_reintento_ciego(pasarela):
    ajustes.modo_cobro = "timeout"
    clave = f"K2-{uuid.uuid4()}"
    pedido, r1 = _cobro(clave=clave)
    assert r1.json()["estado"] == "INCIERTO"
    assert _cobro(pedido, clave=clave)[1].json()["estado"] == "INCIERTO"           # misma K2: devuelve lo guardado
    assert _cobro(pedido)[1].status_code == 409                                    # otra K2: PAGO_EN_CURSO
    assert pasarela["cobrar"] == 1


def test_at05_callback_duplicado_y_contradictorio(pasarela):
    ajustes.modo_cobro = "timeout"
    _, r = _cobro()
    pago = r.json()["pago_id"]
    n1 = {"pago_id": pago, "notificacion_id": f"N1-{uuid.uuid4()}", "resultado": "APROBADO"}
    assert cliente.post("/v1/notificaciones", headers=S, json=n1).json()["estado"] == "APROBADO"
    assert cliente.post("/v1/notificaciones", headers=S, json=n1).json()["estado"] == "APROBADO"
    otra = {"pago_id": pago, "notificacion_id": f"N1-{uuid.uuid4()}", "resultado": "RECHAZADO"}
    assert cliente.post("/v1/notificaciones", headers=S, json=otra).json()["estado"] == "APROBADO"
    with tx() as conn:
        conflicto = conn.execute("SELECT conflicto FROM pagos.notificacion_pago WHERE notificacion_id = %s",
                                 (otra["notificacion_id"],)).fetchone()["conflicto"]
    assert _eventos(pago, "PagoAprobado") == 1 and conflicto is True


def test_at09_reembolso_parcial_repetido_se_aplica_una_vez(pasarela):
    pedido, _ = _cobro(monto="125.00")
    clave = f"K4-{uuid.uuid4()}"
    datos = {"pedido_id": pedido, "componente": "MERCANCIA", "tipo": "PARCIAL", "monto": "45.00", "motivo": "FALTANTE"}
    r1 = cliente.post("/v1/ajustes", headers={**S, "Idempotency-Key": clave}, json=datos)
    r2 = cliente.post("/v1/ajustes", headers={**S, "Idempotency-Key": clave}, json=datos)
    assert r1.json()["estado"] == "APLICADO" and r2.json()["repetida"] is True
    assert pasarela["reembolsar"] == 1
    with tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM pagos.ajuste_cobro WHERE pedido_id = %s", (pedido,)).fetchone()["n"]
    assert n == 1
    exceso = {**datos, "monto": "90.00"}                                          # 45 + 90 > 125 cobrados
    assert cliente.post("/v1/ajustes", headers={**S, "Idempotency-Key": f"K4-{uuid.uuid4()}"}, json=exceso).status_code == 422


def test_sin_reembolso_parcial_finanzas_resuelve_con_evidencia(pasarela):
    ajustes.modo_reembolso = "sin_parcial"
    pedido, _ = _cobro(monto="125.00")
    datos = {"pedido_id": pedido, "componente": "MERCANCIA", "tipo": "PARCIAL", "monto": "20.00", "motivo": "FALTANTE"}
    r = cliente.post("/v1/ajustes", headers={**S, "Idempotency-Key": f"K4-{uuid.uuid4()}"}, json=datos)
    assert r.json()["estado"] == "PENDIENTE_MANUAL"
    fin = "Bearer " + jwt.encode({"sub": "finanzas-1", "roles": ["FINANZAS"]}, "prueba", algorithm="HS256")
    res = {"ajuste_id": r.json()["ajuste_id"], "resultado": "APLICADO", "referencia_externa": "TRF-001",
           "evidencia": "comprobante de transferencia manual"}
    assert cliente.post("/v1/resoluciones", headers={"Authorization": fin}, json=res).json()["estado"] == "APLICADO"
    cli = "Bearer " + jwt.encode({"sub": "c", "roles": ["CLIENTE"]}, "prueba", algorithm="HS256")
    assert cliente.post("/v1/resoluciones", headers={"Authorization": cli}, json=res).status_code == 403
