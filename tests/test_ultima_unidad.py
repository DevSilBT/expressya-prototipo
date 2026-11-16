"""TP-S3-04 · Dos (o cien) clientes por la última unidad, por el código real de la aplicación (M2 → M3 → PostgreSQL)."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.db import transaccion
from app.pedidos import servicio


def _checkout(_):
    return servicio.checkout(f"K1-{uuid.uuid4()}", str(uuid.uuid4()), "53150", [{"sku": "SKU-X", "cantidad": 1}],
                             Decimal("0"), Decimal("0"), Decimal("0"), str(uuid.uuid4()))


def test_cien_clientes_ultima_unidad():
    with transaccion() as conn:   # la tienda 2 también cubre 53150: se agota para que todos compitan en la tienda 1
        conn.execute("UPDATE inventario.existencia SET vendible = 0 WHERE tienda_id = 2 AND sku = 'SKU-X'")
    with ThreadPoolExecutor(max_workers=50) as ex:
        resultados = list(ex.map(_checkout, range(100)))
    aceptados = [r for r in resultados if r["reserva"]["resultado"] == "ACEPTADA"]
    rechazados = [r for r in resultados if r["reserva"]["resultado"] == "AGOTADO"]
    assert len(aceptados) == 1 and len(rechazados) == 99
    assert aceptados[0]["estado"] == "RESERVADO" and all(r["estado"] == "ESPERANDO_DECISION" for r in rechazados)
    with transaccion() as conn:
        assert conn.execute("SELECT inventario.disponible(1::smallint, 'SKU-X') AS d").fetchone()["d"] == 0
        assert conn.execute("SELECT count(*) AS n FROM inventario.solicitud_reserva WHERE tienda_id = 1").fetchone()["n"] >= 100
