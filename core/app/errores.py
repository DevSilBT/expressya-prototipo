"""Errores de negocio con formato application/problem+json (RFC 9457)."""
from fastapi import Request
from fastapi.responses import JSONResponse

ESTADO_HTTP = {
    "AGOTADO": 409, "SKU_BLOQUEADO": 409, "RESERVA_VENCIDA": 409, "RESERVA_NO_ACTIVA": 409, "PAGO_EN_CURSO": 409,
    "ESTADO_INVALIDO": 409, "CLAVE_REUTILIZADA": 422, "CANTIDAD_INVALIDA": 422, "MOTIVO_REQUERIDO": 422,
    "SIN_COBERTURA": 422, "TIENDA_DISTINTA": 422, "NO_AUTORIZADO": 403, "NO_AUTENTICADO": 401,
    "NO_ENCONTRADO": 404, "PAGOS_NO_DISPONIBLE": 503,
}


class ErrorNegocio(Exception):
    def __init__(self, codigo: str, detalle: str = "", datos: dict | None = None):
        super().__init__(codigo)
        self.codigo, self.detalle, self.datos = codigo, detalle, datos or {}


async def manejar_error_negocio(request: Request, exc: ErrorNegocio) -> JSONResponse:
    estado = ESTADO_HTTP.get(exc.codigo, 400)
    cuerpo = {
        "type": f"https://expressya.example/errores/{exc.codigo.lower()}",
        "title": exc.codigo,
        "status": estado,
        "code": exc.codigo,
        "detail": exc.detalle,
        "correlacion_id": getattr(request.state, "correlacion_id", None),
        **exc.datos,
    }
    return JSONResponse(cuerpo, status_code=estado, media_type="application/problem+json")
