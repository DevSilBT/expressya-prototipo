"""Acceso a PostgreSQL. READ COMMITTED (valor por defecto) + bloqueos de fila dentro de las funciones de M3 (ADR-18)."""
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import ajustes

# lock_timeout evita esperas indefinidas en SKU muy disputados; statement_timeout acota cualquier consulta.
pool = ConnectionPool(
    conninfo=ajustes.database_url,
    min_size=1,
    max_size=ajustes.pool_max,
    kwargs={"row_factory": dict_row, "options": "-c lock_timeout=3s -c statement_timeout=10s -c TimeZone=UTC"},
    open=False,
)


@contextmanager
def transaccion():
    """Una transacción por operación de negocio: todo o nada (incluye el evento en comun.outbox)."""
    with pool.connection() as conn:
        with conn.transaction():
            yield conn
