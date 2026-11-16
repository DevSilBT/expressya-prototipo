import os


class Ajustes:
    database_url: str = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/expressya_pagos")
    token_servicio: str = os.environ.get("TOKEN_SERVICIO", "solo-desarrollo")
    jwt_secreto: str = os.environ.get("JWT_SECRETO", "solo-desarrollo")
    core_url: str = os.environ.get("CORE_URL", "http://localhost:8000")
    # Comportamiento de la pasarela SIMULADA (no hay pasarela real en el MVP; Cliente 1-nov, punto 17)
    modo_cobro: str = os.environ.get("PASARELA_MODO_COBRO", "aprobar")          # aprobar | rechazar | timeout
    modo_reembolso: str = os.environ.get("PASARELA_MODO_REEMBOLSO", "aplicar")  # aplicar | sin_parcial | timeout
    minutos_alerta_finanzas: int = int(os.environ.get("MINUTOS_ALERTA_FINANZAS", "30"))


ajustes = Ajustes()
