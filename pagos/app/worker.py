"""Worker de Pagos: entrega la bandeja de salida a Pedidos y alerta a Finanzas a los 30 min (Cliente 1-nov, puntos 13 y 20)."""
import logging
import time

import httpx
from psycopg.types.json import Jsonb

from .config import ajustes
from .main import pool, tx

log = logging.getLogger("pagos.worker")


def alertar_inciertos() -> int:
    with tx() as conn:
        filas = conn.execute(
            """UPDATE pagos.pago SET estado = 'EN_REVISION_FINANZAS', alerta_enviada_en = clock_timestamp()
                WHERE estado = 'INCIERTO' AND solicitado_en < clock_timestamp() - make_interval(mins => %s)
                RETURNING pago_id::text, pedido_id::text""", (ajustes.minutos_alerta_finanzas,)).fetchall()
        for f in filas:
            conn.execute("INSERT INTO pagos.outbox (tipo, agregado_id, datos) VALUES ('PagoEnRevisionFinanzas', %s, %s)",
                         (f["pago_id"], Jsonb({**f, "estado": "EN_REVISION_FINANZAS"})))
            log.warning("ALERTA_FINANZAS pago incierto %s (no se promete resolución en 30 min)", f["pago_id"])
        conn.execute(
            """UPDATE pagos.ajuste_cobro SET estado = 'EN_REVISION_FINANZAS'
                WHERE estado = 'INCIERTO' AND creado_en < clock_timestamp() - make_interval(mins => %s)""",
            (ajustes.minutos_alerta_finanzas,))
    return len(filas)


def entregar(http: httpx.Client) -> int:
    n = 0
    with tx() as conn:
        for e in conn.execute("""SELECT * FROM pagos.outbox WHERE entregado_en IS NULL
                                  ORDER BY creado_en LIMIT 100 FOR UPDATE SKIP LOCKED""").fetchall():
            ruta = "/internal/v1/eventos-pago" if e["tipo"].startswith("Pago") else "/internal/v1/eventos-ajuste"
            try:
                http.post(ruta, json=e["datos"]).raise_for_status()
            except httpx.HTTPError as exc:   # se reintenta; Pedidos deduplica por notificacion_id
                conn.execute("UPDATE pagos.outbox SET intentos = intentos + 1 WHERE evento_id = %s", (e["evento_id"],))
                log.warning("entrega pendiente %s: %s", e["evento_id"], exc)
                continue
            conn.execute("UPDATE pagos.outbox SET entregado_en = clock_timestamp() WHERE evento_id = %s", (e["evento_id"],))
            n += 1
    return n


def una_pasada(http: httpx.Client) -> dict:
    return {"alertas": alertar_inciertos(), "entregados": entregar(http)}


def _cliente() -> httpx.Client:
    return httpx.Client(base_url=ajustes.core_url, headers={"X-Servicio-Token": ajustes.token_servicio}, timeout=30)


def una_vez():
    """Modo job programado (Azure Container Apps Jobs): una pasada y termina."""
    pool.open(wait=True)
    try:
        with _cliente() as http:
            log.info("pasada %s", una_pasada(http))
    finally:
        pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pool.open(wait=True)
    with _cliente() as http:
        while True:
            una_pasada(http)
            time.sleep(5)
