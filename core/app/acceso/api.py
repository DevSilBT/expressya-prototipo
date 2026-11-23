"""C0 · Sesión de DEMOSTRACIÓN (registro simulado). Emite un JWT para un rol elegido, sin contraseña.
Solo existe fuera de producción: con AMBIENTE=produccion responde 404. El inicio de sesión real queda pendiente
(el SDD prevé un proveedor de identidad, ADR-25)."""
import time
import uuid
from typing import Literal

import jwt
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..config import ajustes
from ..errores import ErrorNegocio

router = APIRouter(prefix="/v1/demo", tags=["Demostración (C0 simulado)"])
ROLES = Literal["CLIENTE", "PICKER", "SUPERVISOR", "ENCARGADO", "ATENCION", "AUDITOR"]


class Registro(BaseModel):
    nombre: str = Field(min_length=2, max_length=80)
    correo: str | None = Field(default=None, max_length=120)
    rol: ROLES = "CLIENTE"
    usuario_id: str | None = None          # para volver a entrar con la misma identidad simulada
    tienda_id: int | None = Field(default=None, ge=1, le=99)


@router.post("/sesion", summary="Registro / sesión simulada para la demostración (no disponible en producción)")
def sesion(r: Registro):
    if ajustes.ambiente.lower() in ("produccion", "producción", "prod"):
        raise ErrorNegocio("NO_ENCONTRADO", "No disponible en producción")
    try:
        sub = str(uuid.UUID(r.usuario_id)) if r.usuario_id else str(uuid.uuid4())
    except ValueError as exc:
        raise ErrorNegocio("IDENTIDAD_INVALIDA", "usuario_id debe ser un UUID") from exc
    exp = int(time.time()) + 8 * 3600
    claims = {"sub": sub, "roles": [r.rol], "nombre": r.nombre, "exp": exp, "demo": True}
    if r.tienda_id:
        claims["tienda_id"] = r.tienda_id
    return {"token": "Bearer " + jwt.encode(claims, ajustes.jwt_secreto, algorithm="HS256"),
            "usuario_id": sub, "rol": r.rol, "nombre": r.nombre, "expira_en": exp}
