"""Proceso worker del monolito: barrido de expiración, entrega de la bandeja de salida y ciclo de reparación (ADR-12).
Se ejecuta como proceso aparte: `python -m app.worker`. Varias instancias son seguras (FOR UPDATE SKIP LOCKED)."""
import logging
import time
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .config import ajustes
from .db import pool, transaccion
from .inventario import servicio as inventario
from .pedidos import servicio as pedidos
from .pedidos.cliente_pagos import cliente_pagos

log = logging.getLogger("worker")


def entregar_outbox(limite: int = 200) -> int:
    """Consume comun.outbox: bitácora (M7), efectos internos y llamadas a Pagos. Al menos una vez; consumidores idempotentes."""
    with transaccion() as conn:
        eventos = conn.execute(
            """SELECT * FROM comun.outbox WHERE entregado_en IS NULL ORDER BY creado_en
               LIMIT %s FOR UPDATE SKIP LOCKED""", (limite,)).fetchall()
        for e in eventos:
            datos = e["datos"]
            conn.execute(
                """INSERT INTO trazabilidad.evento_bitacora (evento_id, tipo, correlacion_id, pedido_id, tienda_id, sku, actor, hora_servidor, datos)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (evento_id) DO NOTHING""",
                (e["evento_id"], e["tipo"], e["correlacion_id"], datos.get("pedido_id"), datos.get("tienda_id"),
                 datos.get("sku"), e["modulo"], e["creado_en"], Jsonb(datos)))
            if e["tipo"] == "ReservaExpirada":
                conn.execute("UPDATE pedidos.pedido SET estado = 'NO_PAGADO' WHERE pedido_id = %s AND estado = 'RESERVADO'",
                             (datos["pedido_id"],))
            elif e["tipo"] == "SolicitudAjusteCobro":
                try:
                    cliente_pagos.solicitar_ajuste({k: datos[k] for k in ("pedido_id", "componente", "tipo", "monto", "motivo")},
                                                   datos["clave"], e["correlacion_id"] and str(e["correlacion_id"]))
                except Exception as exc:   # Pagos no disponible: se reintenta con la MISMA K4 en el siguiente ciclo
                    conn.execute("UPDATE comun.outbox SET intentos = intentos + 1 WHERE evento_id = %s", (e["evento_id"],))
                    log.warning("ajuste pendiente %s: %s", datos["clave"], exc)
                    continue
            conn.execute("UPDATE comun.outbox SET entregado_en = clock_timestamp() WHERE evento_id = %s", (e["evento_id"],))
        return len(eventos)


def una_pasada(con_reparacion: bool = True) -> dict:
    """Una vuelta completa del worker. La usan el bucle local (docker compose) y el job programado de Azure."""
    with transaccion() as conn:
        n = inventario.expirar_vencidas(conn)
    entregados = entregar_outbox()
    reparados = None
    if con_reparacion:
        desde = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        try:
            reparados = pedidos.reparar(desde)
        except Exception as exc:
            reparados = -1
            log.warning("reparación no disponible: %s", exc)
    log.info("barrido=%s outbox=%s reparados=%s", n, entregados, reparados)
    return {"barrido": n, "outbox": entregados, "reparados": reparados}


def ciclo():
    """Modo continuo (local y App Platform): barrido cada PERIODO_BARRIDO_S, reparación cada PERIODO_REPARACION_S."""
    pool.open(wait=True)
    ultima_reparacion = 0.0
    while True:
        toca = time.monotonic() - ultima_reparacion >= ajustes.periodo_reparacion_s
        una_pasada(con_reparacion=toca)
        if toca:
            ultima_reparacion = time.monotonic()
        time.sleep(ajustes.periodo_barrido_s)


def una_vez():
    """Modo job programado (Azure Container Apps Jobs): una pasada y termina."""
    pool.open(wait=True)
    try:
        una_pasada(con_reparacion=True)
    finally:
        pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ciclo()
