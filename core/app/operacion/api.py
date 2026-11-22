"""API de M5 · Operación (C3). Roles según la matriz del Anexo B §26.2; la separación de funciones la impone la base."""
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from ..db import transaccion
from ..pedidos.api import _clave, _verificar_dueno
from ..pedidos import servicio as pedidos
from ..seguridad import Usuario, requiere_rol
from . import servicio

router = APIRouter(prefix="/v1", tags=["Operación (M5) · C3"])
OPERACION = ("PICKER", "SUPERVISOR", "ENCARGADO")


class Surtido(BaseModel):
    cantidad: int = Field(gt=0, le=99)


class Faltante(BaseModel):
    encontradas: int = Field(ge=0, le=99)
    faltantes: int = Field(gt=0, le=99)
    motivo: Literal["NO_ENCONTRADO", "DANADO_EN_ANAQUEL", "CADUCADO", "UBICACION_INCORRECTA", "OTRO"]
    descripcion: str | None = Field(default=None, max_length=500)


class Validacion(BaseModel):
    valido: bool
    evidencia: str = Field(min_length=3, max_length=1000)


class Conteo(BaseModel):
    cantidad_fisica: int = Field(ge=0, le=100000)


class Autorizacion(BaseModel):
    aprobar: bool
    evidencia: str = Field(min_length=3, max_length=1000)


@router.post("/pedidos/{pedido_id}/preparacion", summary="API-OPS-01 IniciarPreparacion (PAGADO → EN_PREPARACION)")
def iniciar_preparacion(pedido_id: str, request: Request, usuario: Usuario = Depends(requiere_rol(*OPERACION))):
    return servicio.iniciar_preparacion(pedido_id, usuario.usuario_id, request.state.correlacion_id)


@router.post("/lineas/{linea_id}/surtido", summary="API-OPS-02 SurtirLinea (descuenta vendible y comprometido, ADR-02)")
def surtir(linea_id: str, cuerpo: Surtido, request: Request, idempotency_key: str = Header(...),
           usuario: Usuario = Depends(requiere_rol("PICKER"))):
    return servicio.surtir(linea_id, cuerpo.cantidad, usuario.usuario_id, _clave(idempotency_key), request.state.correlacion_id)


@router.post("/lineas/{linea_id}/faltantes", status_code=201, summary="API-OPS-03 ReportarFaltante (bloquea la venta del SKU)")
def reportar_faltante(linea_id: str, cuerpo: Faltante, request: Request, idempotency_key: str = Header(...),
                      usuario: Usuario = Depends(requiere_rol("PICKER"))):
    return servicio.reportar_faltante(linea_id, cuerpo.encontradas, cuerpo.faltantes, cuerpo.motivo, cuerpo.descripcion,
                                      usuario.usuario_id, _clave(idempotency_key), request.state.correlacion_id)


@router.post("/faltantes/{faltante_id}/validacion",
             summary="API-OPS-04 ValidarFaltante (supervisor ≠ picker; cancelación parcial y reembolso de la mercancía)")
def validar_faltante(faltante_id: str, cuerpo: Validacion, request: Request, idempotency_key: str = Header(...),
                     usuario: Usuario = Depends(requiere_rol("SUPERVISOR"))):
    return servicio.validar_faltante(faltante_id, usuario.usuario_id, cuerpo.valido, cuerpo.evidencia,
                                     _clave(idempotency_key), request.state.correlacion_id)


@router.post("/tiendas/{tienda_id}/existencias/{sku}/conteos", status_code=201,
             summary="API-OPS-05 RegistrarConteo (diferencia → discrepancia, SKU bloqueado y ajuste por autorizar)")
def registrar_conteo(tienda_id: int, sku: str, cuerpo: Conteo, request: Request, idempotency_key: str = Header(...),
                     usuario: Usuario = Depends(requiere_rol(*OPERACION))):
    return servicio.registrar_conteo(tienda_id, sku, cuerpo.cantidad_fisica, usuario.usuario_id, _clave(idempotency_key),
                                     request.state.correlacion_id)


@router.post("/ajustes-inventario/{ajuste_id}/autorizacion", summary="API-OPS-06 AutorizarAjuste (autorizador ≠ solicitante)")
def autorizar_ajuste(ajuste_id: str, cuerpo: Autorizacion, request: Request, idempotency_key: str = Header(...),
                     usuario: Usuario = Depends(requiere_rol("SUPERVISOR", "ENCARGADO"))):
    return servicio.autorizar_ajuste(ajuste_id, usuario.usuario_id, cuerpo.aprobar, cuerpo.evidencia,
                                     _clave(idempotency_key), request.state.correlacion_id)


@router.get("/tiendas/{tienda_id}/existencias/{sku}/historial",
            summary="API-INV-12 HistorialExistencia (quién, qué, cuándo, por qué y pedido afectado)")
def historial_existencia(tienda_id: int, sku: str, limite: int = Query(200, ge=1, le=1000),
                         usuario: Usuario = Depends(requiere_rol("AUDITOR", "SUPERVISOR", "ENCARGADO"))):
    return servicio.historial_existencia(tienda_id, sku, limite)


@router.get("/pedidos/{pedido_id}/historial", summary="API-TRZ-01 HistorialPedido (impacto del inventario en el pedido)")
def historial_pedido(pedido_id: str, usuario: Usuario = Depends(requiere_rol("CLIENTE", "ATENCION", "SUPERVISOR", "AUDITOR"))):
    with transaccion() as conn:
        _verificar_dueno(conn, pedido_id, usuario)
        return {**pedidos.consultar(conn, pedido_id), **servicio.historial_pedido(conn, pedido_id)}
