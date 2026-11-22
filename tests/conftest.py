"""Pruebas automatizadas contra PostgreSQL real. Requiere PostgreSQL 16 accesible:
   TEST_PG_ADMIN_URL=postgresql://postgres@localhost:5432/postgres pytest -q
Crea una base temporal, carga el DDL y la elimina al terminar."""
import os
import pathlib
import sys
import uuid

import psycopg
import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
ADMIN = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres@localhost:5432/postgres")
NOMBRE = f"expressya_test_{uuid.uuid4().hex[:8]}"
URL = ADMIN.rsplit("/", 1)[0] + "/" + NOMBRE

os.environ["DATABASE_URL"] = URL          # antes de importar la app (12-factor)
os.environ["DB_POOL_MAX"] = "30"
os.environ["JWT_SECRETO"] = "prueba"
sys.path.insert(0, str(RAIZ / "core"))


@pytest.fixture(scope="session", autouse=True)
def base_de_datos():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {NOMBRE}")
    with psycopg.connect(URL, autocommit=True) as c:
        for f in ("001_esquemas_y_tablas.sql", "002_funciones_inventario.sql", "003_datos_demo.sql", "004_carga_y_reconstruccion.sql"):
            c.execute((RAIZ / "db" / "core" / f).read_text())
    from app.db import pool
    pool.open(wait=True)
    yield URL
    pool.close()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE {NOMBRE} WITH (FORCE)")


@pytest.fixture
def token_cliente():
    import jwt
    return "Bearer " + jwt.encode({"sub": str(uuid.uuid4()), "roles": ["CLIENTE"]}, "prueba", algorithm="HS256")


class PagosFalso:
    """Sustituye al servicio de Pagos: permite simular aprobación, rechazo, timeout y pérdida de eventos."""
    def __init__(self):
        self.modo, self.llamadas_cobro, self.finales = "APROBADO", [], []

    def solicitar_cobro(self, pedido_id, monto, clave_k2, corr):
        self.llamadas_cobro.append(clave_k2)
        if self.modo == "TIMEOUT":
            return {"estado": "INCIERTO", "motivo": "TIMEOUT_HACIA_PAGOS"}
        return {"pago_id": str(uuid.uuid4()), "estado": self.modo, "notificacion_id": f"N1-{uuid.uuid4()}"}

    def solicitar_ajuste(self, datos, clave_k4, corr):
        return {"estado": "APLICADO"}

    def pagos_finales(self, desde):
        return self.finales


@pytest.fixture
def pagos_falso(monkeypatch):
    from app.pedidos import servicio
    falso = PagosFalso()
    monkeypatch.setattr(servicio, "cliente_pagos", falso)
    return falso
