"""Pasarela simulada. Sin garantías de consulta, reembolso parcial ni idempotencia nativa (Cliente 1-nov, punto 17).
La idempotencia la pone el servicio de Pagos, no la pasarela."""
import uuid
from datetime import datetime, timezone


class TimeoutPasarela(Exception):
    pass


class ReembolsoParcialNoSoportado(Exception):
    pass


def cobrar(monto: str, modo: str) -> dict:
    if modo == "timeout":
        raise TimeoutPasarela()
    return {"resultado": "APROBADO" if modo == "aprobar" else "RECHAZADO",
            "notificacion_id": f"N1-{uuid.uuid4()}", "referencia": f"PS-{uuid.uuid4().hex[:12]}",
            "aprobado_en": datetime.now(timezone.utc).isoformat()}


def reembolsar(monto: str, parcial: bool, modo: str) -> dict:
    if modo == "timeout":
        raise TimeoutPasarela()
    if parcial and modo == "sin_parcial":
        raise ReembolsoParcialNoSoportado()
    return {"referencia": f"RF-{uuid.uuid4().hex[:12]}"}
