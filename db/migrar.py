"""Migrador de esquema para ambientes administrados (Azure Database for PostgreSQL Flexible Server).

En local, docker compose carga el DDL con db/inicializar.sh. En la nube no hay superusuario, así que este programa:
  1. Con el usuario administrador del servidor (ADMIN_URL), crea dos roles de aplicación con mínimo privilegio
     (expressya_core y expressya_pagos, ADR-25) y una base para cada uno, de la que cada rol es dueño (ADR-17).
  2. Crea la extensión pgcrypto en cada base (en Azure requiere el parámetro azure.extensions = PGCRYPTO).
  3. Conectado como el rol dueño, aplica en orden los archivos .sql que aún no se aplicaron, cada uno en su transacción,
     y lo registra en migracion.aplicada. Volver a ejecutarlo no repite nada (idempotente).

Variables: ADMIN_URL, CORE_DB_PASSWORD, PAGOS_DB_PASSWORD, CARGAR_DATOS_DEMO (1 por defecto).
Uso: python migrar.py
"""
import os
import pathlib
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

RAIZ = pathlib.Path(__file__).resolve().parent
ADMIN_URL = os.environ["ADMIN_URL"]
DEMO = os.environ.get("CARGAR_DATOS_DEMO", "1") == "1"

BASES = [
    # (base, rol, variable con la contraseña del rol, carpeta con el DDL)
    ("expressya", "expressya_core", "CORE_DB_PASSWORD", "core"),
    ("expressya_pagos", "expressya_pagos", "PAGOS_DB_PASSWORD", "pagos"),
]


def url(base: str, usuario: str | None = None, clave: str | None = None) -> str:
    extra = {"dbname": base}
    if usuario:
        extra.update(user=usuario, password=clave)
    return make_conninfo(ADMIN_URL, **extra)


def preparar(admin: psycopg.Connection, base: str, rol: str, clave: str) -> None:
    existe = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (rol,)).fetchone()
    if existe:
        admin.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(rol), sql.Literal(clave)))
    else:
        admin.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(rol), sql.Literal(clave)))
    # PostgreSQL 16: para crear una base cuyo dueño es otro rol, el administrador debe poder asumir ese rol.
    admin.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(rol)))
    if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (base,)).fetchone():
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(base), sql.Identifier(rol)))
        print(f"base {base} creada (dueño {rol})")
    with psycopg.connect(url(base), autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        # En Azure el esquema public de las bases nuevas pertenece a azure_pg_admin: el rol dueño de la base no puede
        # crear en él. El DDL usa sus propios esquemas; aquí solo se garantiza el uso de public (pgcrypto vive ahí).
        try:
            c.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(rol)))
        except psycopg.Error as exc:
            print(f"aviso: no se pudo otorgar USAGE en public a {rol}: {exc}")


def aplicar(base: str, rol: str, clave: str, carpeta: str) -> int:
    archivos = sorted((RAIZ / carpeta).glob("*.sql"))
    if not DEMO:
        archivos = [a for a in archivos if "datos_demo" not in a.name]
    n = 0
    with psycopg.connect(url(base, rol, clave), autocommit=True) as c:
        # Registro de migraciones en un esquema propio del rol (no en public; ver preparar()).
        c.execute("CREATE SCHEMA IF NOT EXISTS migracion")
        c.execute("""CREATE TABLE IF NOT EXISTS migracion.aplicada (
                         archivo text PRIMARY KEY, aplicada_en timestamptz NOT NULL DEFAULT clock_timestamp())""")
        hechas = {r[0] for r in c.execute("SELECT archivo FROM migracion.aplicada").fetchall()}
        for a in archivos:
            nombre = f"{carpeta}/{a.name}"
            if nombre in hechas:
                continue
            with c.transaction():
                c.execute(a.read_text(encoding="utf-8"))     # sin parámetros: admite varias sentencias y bloques $$
                c.execute("INSERT INTO migracion.aplicada (archivo) VALUES (%s)", (nombre,))
            print(f"aplicado {nombre}")
            n += 1
    return n


def main() -> int:
    servidor = conninfo_to_dict(ADMIN_URL).get("host")
    print(f"migrando en {servidor}")
    with psycopg.connect(url("postgres"), autocommit=True) as admin:
        for base, rol, var, _ in BASES:
            preparar(admin, base, rol, os.environ[var])
    total = sum(aplicar(base, rol, os.environ[var], carpeta) for base, rol, var, carpeta in BASES)
    print(f"migración terminada: {total} archivo(s) nuevo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
