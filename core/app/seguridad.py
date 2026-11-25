"""C0 · Autenticación (JWT HS256 emitido por el módulo de acceso) y autorización por rol (matriz §26.2 de S2)."""
import hmac
import logging
import time
import uuid

import jwt
from fastapi import Depends, Header, Request

from .config import ajustes
from .errores import ErrorNegocio


_log = logging.getLogger("http")


class Usuario:
    def __init__(self, usuario_id: str, roles: list[str], tienda_id: int | None):
        self.usuario_id, self.roles, self.tienda_id = usuario_id, roles, tienda_id


def usuario_actual(authorization: str = Header(default="")) -> Usuario:
    if not authorization.startswith("Bearer "):
        raise ErrorNegocio("NO_AUTENTICADO", "Falta el token Bearer")
    try:
        datos = jwt.decode(authorization[7:], ajustes.jwt_secreto, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise ErrorNegocio("NO_AUTENTICADO", str(e)) from e
    return Usuario(datos["sub"], datos.get("roles", []), datos.get("tienda_id"))


def requiere_rol(*roles: str):
    def dependencia(usuario: Usuario = Depends(usuario_actual)) -> Usuario:
        if not set(roles) & set(usuario.roles):
            raise ErrorNegocio("NO_AUTORIZADO", f"Se requiere uno de los roles {roles}")
        return usuario
    return dependencia


def requiere_servicio(x_servicio_token: str = Header(default="")) -> str:
    """Llamadas internas pagos -> core. Comparación en tiempo constante."""
    if not hmac.compare_digest(x_servicio_token, ajustes.token_servicio):
        raise ErrorNegocio("NO_AUTORIZADO", "Token de servicio inválido")
    return "SERVICIO_PAGOS"


async def middleware_correlacion(request: Request, call_next):
    """Asigna o propaga X-Correlation-Id (RNF07) y lo devuelve en la respuesta."""
    corr = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    request.state.correlacion_id = corr
    t0 = time.perf_counter()
    respuesta = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    respuesta.headers["X-Correlation-Id"] = corr
    # Tiempo dentro del servidor: el total que mide el cliente menos este valor = red + arranque en frío + TLS
    respuesta.headers["Server-Timing"] = f"app;dur={ms:.1f}"
    _log.info("solicitud metodo=%s ruta=%s estado=%s ms=%.1f corr=%s", request.method, request.url.path,
              respuesta.status_code, ms, corr)
    return respuesta
