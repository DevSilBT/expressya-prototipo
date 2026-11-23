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


# ---------------------------------------------------------------- consultas para la interfaz (bandejas de trabajo)
@router.get("/tiendas/{tienda_id}/pedidos", summary="Bandeja del picker: pedidos pagados o en preparación de la tienda")
def pedidos_de_tienda(tienda_id: int, estado: list[str] = Query(["PAGADO", "EN_PREPARACION"]),
                      usuario: Usuario = Depends(requiere_rol(*OPERACION, "AUDITOR"))):
    with transaccion() as conn:
        return {"pedidos": conn.execute(
            """SELECT p.pedido_id, p.estado, p.hora_confirmacion, p.creado_en, p.total,
                      count(l.*) AS lineas,
                      sum(l.cantidad_pedida - l.cantidad_surtida - l.cantidad_cancelada) AS unidades_pendientes
                 FROM pedidos.pedido p JOIN pedidos.linea_pedido l USING (pedido_id)
                WHERE p.tienda_id = %s AND p.estado = ANY(%s)
                GROUP BY p.pedido_id ORDER BY p.hora_confirmacion NULLS LAST, p.creado_en LIMIT 50""",
            (tienda_id, estado)).fetchall()}


@router.get("/tiendas/{tienda_id}/faltantes", summary="Bandeja del supervisor: faltantes por validar")
def faltantes_de_tienda(tienda_id: int, usuario: Usuario = Depends(requiere_rol("SUPERVISOR", "ENCARGADO", "AUDITOR"))):
    with transaccion() as conn:
        return {"faltantes": conn.execute(
            """SELECT f.faltante_id, f.estado, f.motivo, f.encontradas, f.faltantes, f.descripcion, f.picker_id, f.creado_en,
                      l.sku, l.pedido_id
                 FROM operacion.reporte_faltante f JOIN pedidos.linea_pedido l USING (linea_id)
                 JOIN pedidos.pedido p USING (pedido_id)
                WHERE p.tienda_id = %s AND f.estado = 'POR_VALIDAR' ORDER BY f.creado_en""", (tienda_id,)).fetchall()}


@router.get("/tiendas/{tienda_id}/ajustes-inventario", summary="Bandeja del supervisor: ajustes por autorizar")
def ajustes_de_tienda(tienda_id: int, usuario: Usuario = Depends(requiere_rol("SUPERVISOR", "ENCARGADO", "AUDITOR"))):
    with transaccion() as conn:
        return {"ajustes": conn.execute(
            """SELECT a.ajuste_inv_id AS ajuste_id, a.sku, a.tipo, a.cantidad, a.motivo, a.solicitante_id, a.creado_en,
                      d.cantidad_logica, d.cantidad_fisica
                 FROM operacion.ajuste_inventario a LEFT JOIN inventario.discrepancia d USING (discrepancia_id)
                WHERE a.tienda_id = %s AND a.estado = 'PENDIENTE_AUTORIZACION' ORDER BY a.creado_en""",
            (tienda_id,)).fetchall()}


@router.get("/clientes/me/pedidos", summary="Mis pedidos (cliente)")
def mis_pedidos(usuario: Usuario = Depends(requiere_rol("CLIENTE"))):
    with transaccion() as conn:
        return {"pedidos": conn.execute(
            """SELECT pedido_id, estado, tienda_id, total, creado_en, actualizado_en FROM pedidos.pedido
                WHERE cliente_id = %s ORDER BY creado_en DESC LIMIT 30""", (servicio._uuid(usuario.usuario_id),)).fetchall()}


@router.get("/tiendas/{tienda_id}/pedidos/{pedido_id}", summary="Detalle del pedido para el personal de la tienda")
def pedido_de_tienda(tienda_id: int, pedido_id: str, usuario: Usuario = Depends(requiere_rol(*OPERACION, "AUDITOR"))):
    with transaccion() as conn:
        p = pedidos.consultar(conn, servicio._uuid(pedido_id, "pedido_id"))
        if p["tienda_id"] != tienda_id:
            raise servicio.ErrorNegocio("NO_ENCONTRADO", "El pedido no pertenece a esta tienda")
        p["faltantes_por_validar"] = conn.execute(
            """SELECT f.faltante_id, l.sku, f.faltantes FROM operacion.reporte_faltante f JOIN pedidos.linea_pedido l USING (linea_id)
                WHERE l.pedido_id = %s AND f.estado = 'POR_VALIDAR'""", (pedido_id,)).fetchall()
        return p
