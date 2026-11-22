"""M3 · Inventario y Reservas (autoridad). Única puerta de escritura a los saldos: funciones SQL transaccionales (ADR-19)."""
from psycopg import Connection
from psycopg.types.json import Jsonb


def disponibilidad(conn: Connection, tienda_id: int, skus: list[str]) -> list[dict]:
    filas = conn.execute(
        """SELECT e.sku, inventario.disponible(e.tienda_id, e.sku) AS disponible,
                  inventario.reservas_vigentes(e.tienda_id, e.sku, clock_timestamp()) AS reservado,
                  e.comprometido, e.vendible, e.estado
             FROM inventario.existencia e WHERE e.tienda_id = %s AND e.sku = ANY(%s) ORDER BY e.sku""",
        (tienda_id, skus),
    ).fetchall()
    return [dict(f) for f in filas]


def solicitar_reserva(conn: Connection, pedido_id, tienda_id: int, lineas: list[dict], clave: str, actor: str, corr) -> dict:
    return conn.execute(
        "SELECT inventario.solicitar_reserva(%s, %s::smallint, %s, %s, %s, %s) AS r",
        (pedido_id, tienda_id, Jsonb(lineas), clave, actor, corr),
    ).fetchone()["r"]


def confirmar_reserva(conn: Connection, reserva_id, clave_n1: str, actor: str, corr) -> dict:
    return conn.execute(
        "SELECT inventario.confirmar_reserva(%s, %s, %s, %s) AS r", (reserva_id, clave_n1, actor, corr)
    ).fetchone()["r"]


def liberar_reserva(conn: Connection, reserva_id, motivo: str, clave: str, actor: str, corr) -> dict:
    return conn.execute(
        "SELECT inventario.liberar_reserva(%s, %s, %s, %s, %s) AS r", (reserva_id, motivo, clave, actor, corr)
    ).fetchone()["r"]


def reserva_del_pedido(conn: Connection, pedido_id) -> dict | None:
    return conn.execute(
        """SELECT reserva_id, estado, expira_en, clock_timestamp() < expira_en AS vigente
             FROM inventario.reserva WHERE pedido_id = %s ORDER BY creada_en DESC LIMIT 1""",
        (pedido_id,),
    ).fetchone()


def expirar_vencidas(conn: Connection, limite: int = 500) -> int:
    return conn.execute("SELECT inventario.expirar_vencidas(NULL, %s) AS n", (limite,)).fetchone()["n"]
