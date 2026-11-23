"""ExpressYa · Monolito modular (M1, M2, M3, M5, M6, M7 + C0) e interfaz web /app · FastAPI. Pagos (M4) es un servicio aparte (ADR-17)."""
import logging

from fastapi import FastAPI

from .db import pool
from .errores import ErrorNegocio, manejar_error_negocio
from .acceso.api import router as acceso_router
from .catalogo.api import router as catalogo_router
from .inventario.api import router as inventario_router
from .operacion.api import router as operacion_router
from .pedidos.api import router as pedidos_router
from .seguridad import middleware_correlacion
from .web.api import router as web_router

logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","nivel":"%(levelname)s","modulo":"%(name)s","msg":"%(message)s"}')

app = FastAPI(title="ExpressYa Core", version="0.5.0",
              description="Prototipo del diseño físico de la Semana 3 · Equipo 02 · Dinamita")
app.middleware("http")(middleware_correlacion)
app.add_exception_handler(ErrorNegocio, manejar_error_negocio)
app.include_router(inventario_router)
app.include_router(pedidos_router)
app.include_router(operacion_router)
app.include_router(catalogo_router)
app.include_router(acceso_router)
app.include_router(web_router)


@app.on_event("startup")
def abrir_pool():
    pool.open(wait=True)


@app.on_event("shutdown")
def cerrar_pool():
    pool.close()


@app.get("/salud", tags=["Operación"])
def salud():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"estado": "ok"}
