"""Pruebas del servicio de Pagos (M4) contra PostgreSQL real, con su propia base temporal (ADR-17).
Se ejecutan aparte de tests/ porque ambos servicios usan el paquete «app»:
   TEST_PG_ADMIN_URL=postgresql://postgres@localhost:5432/postgres pytest -q tests_pagos"""
import os
import pathlib
import sys
import uuid

import psycopg
import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
ADMIN = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres@localhost:5432/postgres")
NOMBRE = f"expressya_pagos_test_{uuid.uuid4().hex[:8]}"
URL = ADMIN.rsplit("/", 1)[0] + "/" + NOMBRE

os.environ["DATABASE_URL"] = URL
os.environ["TOKEN_SERVICIO"] = "token-de-prueba"
os.environ["JWT_SECRETO"] = "prueba"
sys.path.insert(0, str(RAIZ / "pagos"))


@pytest.fixture(scope="session", autouse=True)
def base_de_datos():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {NOMBRE}")
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute((RAIZ / "db" / "pagos" / "001_pagos.sql").read_text())
    from app.main import pool
    pool.open(wait=True)
    yield URL
    pool.close()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE {NOMBRE} WITH (FORCE)")


@pytest.fixture
def pasarela(monkeypatch):
    """Cuenta las llamadas reales a la pasarela simulada y permite cambiar su modo en cada prueba."""
    from app import pasarela_simulada as ps
    from app.config import ajustes
    llamadas = {"cobrar": 0, "reembolsar": 0}
    cobrar, reembolsar = ps.cobrar, ps.reembolsar

    def contar_cobro(*a, **k):
        llamadas["cobrar"] += 1
        return cobrar(*a, **k)

    def contar_reembolso(*a, **k):
        llamadas["reembolsar"] += 1
        return reembolsar(*a, **k)

    monkeypatch.setattr(ps, "cobrar", contar_cobro)
    monkeypatch.setattr(ps, "reembolsar", contar_reembolso)
    monkeypatch.setattr(ajustes, "modo_cobro", "aprobar")
    monkeypatch.setattr(ajustes, "modo_reembolso", "aplicar")
    return llamadas
