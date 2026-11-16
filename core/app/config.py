"""Configuración por variables de entorno (12-factor). Ningún secreto vive en el código."""
import os


class Ajustes:
    database_url: str = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/expressya")
    pool_max: int = int(os.environ.get("DB_POOL_MAX", "20"))
    jwt_secreto: str = os.environ.get("JWT_SECRETO", "solo-desarrollo")
    token_servicio: str = os.environ.get("TOKEN_SERVICIO", "solo-desarrollo")   # core <-> pagos
    pagos_url: str = os.environ.get("PAGOS_URL", "http://localhost:8001")
    timeout_pagos_s: float = float(os.environ.get("TIMEOUT_PAGOS_S", "10"))
    timeout_despertar_pagos_s: float = float(os.environ.get("TIMEOUT_DESPERTAR_PAGOS_S", "60"))  # ST-AZ-04
    periodo_barrido_s: float = float(os.environ.get("PERIODO_BARRIDO_S", "5"))
    periodo_reparacion_s: float = float(os.environ.get("PERIODO_REPARACION_S", "60"))
    ambiente: str = os.environ.get("AMBIENTE", "local")


ajustes = Ajustes()
