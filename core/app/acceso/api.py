"""C0 · Sesión de DEMOSTRACIÓN (registro simulado). Emite un JWT para un rol elegido, sin contraseña.
Solo existe fuera de producción: con AMBIENTE=produccion responde 404. El inicio de sesión real queda pendiente
(el SDD prevé un proveedor de identidad, ADR-25)."""
import time
import uuid
from typing import Literal

import jwt
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import ajustes
from ..db import transaccion
from ..errores import ErrorNegocio
from ..operacion import servicio as operacion
from ..seguridad import Usuario, requiere_rol

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
    with transaccion() as conn:   # nombre visible para la bitácora del personal («quién hizo qué»)
        conn.execute(
            """INSERT INTO acceso.sesion_demo (usuario_id, nombre, rol, tienda_id) VALUES (%s, %s, %s, %s)
               ON CONFLICT (usuario_id) DO UPDATE SET nombre = EXCLUDED.nombre, rol = EXCLUDED.rol,
                                                      tienda_id = EXCLUDED.tienda_id, actualizado_en = clock_timestamp()""",
            (sub, r.nombre, r.rol, r.tienda_id))
    exp = int(time.time()) + 8 * 3600
    claims = {"sub": sub, "roles": [r.rol], "nombre": r.nombre, "exp": exp, "demo": True}
    if r.tienda_id:
        claims["tienda_id"] = r.tienda_id
    return {"token": "Bearer " + jwt.encode(claims, ajustes.jwt_secreto, algorithm="HS256"),
            "usuario_id": sub, "rol": r.rol, "nombre": r.nombre, "expira_en": exp}


def _solo_fuera_de_produccion():
    if ajustes.ambiente.lower() in ("produccion", "producción", "prod"):
        raise ErrorNegocio("NO_ENCONTRADO", "No disponible en producción")


@router.post("/escenario-zeta", summary="Prepara el caso del profesor: Producto Zeta con inventario lógico 3 en la tienda 5 "
                                        "(reposición auditada; no disponible en producción)")
def escenario_zeta(usuario: Usuario = Depends(requiere_rol("ENCARGADO", "SUPERVISOR"))):
    _solo_fuera_de_produccion()
    r = operacion.preparar_escenario(5, "SKU-ZETA", 3, usuario.usuario_id)
    return {**r, "codigo_postal": "53400",
            "guion": "1) Cliente CP 53400 pide 3 × Producto Zeta y paga. 2) Picker de la tienda 5 inicia la preparación, "
                     "pulsa «Encontré menos» y registra 2. 3) Supervisor (otra persona) valida el faltante. "
                     "4) Pedido PARCIAL, reembolso de 1 unidad, inventario y bitácora actualizados."}
