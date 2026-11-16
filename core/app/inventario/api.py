from fastapi import APIRouter, Query

from ..db import transaccion
from . import servicio

router = APIRouter(prefix="/v1", tags=["Inventario (M3)"])


@router.get("/tiendas/{tienda_id}/disponibilidad", summary="API-INV-01 ConsultarDisponibilidad")
def consultar_disponibilidad(tienda_id: int, sku: list[str] = Query(..., min_length=1, max_length=100)):
    with transaccion() as conn:
        return {"tienda_id": tienda_id, "origen": "AUTORIDAD", "skus": servicio.disponibilidad(conn, tienda_id, sku)}
