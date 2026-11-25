import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field, conlist

from ..db import transaccion
from ..errores import ErrorNegocio
from ..seguridad import Usuario, requiere_rol, requiere_servicio
from . import servicio

router = APIRouter(tags=["Pedidos (M2)"])


class LineaEntrada(BaseModel):
    sku: str = Field(pattern=r"^[A-Z0-9-]{3,40}$")
    cantidad: int = Field(gt=0, le=99)


class CheckoutEntrada(BaseModel):
    codigo_postal: str = Field(pattern=r"^[0-9]{5}$")
    lineas: conlist(LineaEntrada, min_length=1, max_length=50)
    envio: Decimal = Field(default=Decimal("0"), ge=0)
    propina: Decimal = Field(default=Decimal("0"), ge=0)
    descuento_promocion: Decimal = Field(default=Decimal("0"), ge=0)


class EventoPago(BaseModel):
    pedido_id: str
    pago_id: str
    estado: str = Field(pattern="^(APROBADO|RECHAZADO|INCIERTO|EN_REVISION_FINANZAS)$")
    notificacion_id: str | None = None
    aprobado_en_pasarela: str | None = None


def _clave(idempotency_key: str) -> str:
    if not 8 <= len(idempotency_key) <= 100:
        raise ErrorNegocio("CANTIDAD_INVALIDA", "Idempotency-Key obligatoria (8 a 100 caracteres)")
    return idempotency_key


@router.post("/v1/pedidos", status_code=201, summary="API-PED-01 IniciarCheckout (asigna tienda y reserva 300 s)")
def iniciar_checkout(cuerpo: CheckoutEntrada, request: Request, idempotency_key: str = Header(...),
                     usuario: Usuario = Depends(requiere_rol("CLIENTE"))):
    resultado = servicio.checkout(_clave(idempotency_key), usuario.usuario_id, cuerpo.codigo_postal,
                                  [l.model_dump() for l in cuerpo.lineas], cuerpo.envio, cuerpo.propina,
                                  cuerpo.descuento_promocion, request.state.correlacion_id)
    reserva = resultado.get("reserva", {})
    if reserva.get("resultado") in ("AGOTADO", "SKU_BLOQUEADO"):
        raise ErrorNegocio(reserva["resultado"], "Ninguna línea se reservó; el cliente decide (RF07)",
                           {"pedido_id": str(resultado["pedido_id"]), "lineas": reserva.get("lineas", [])})
    return resultado


def _verificar_dueno(conn, pedido_id: str, usuario: Usuario):
    """Un CLIENTE solo ve y paga sus propios pedidos; si no es suyo responde 404 (no revela que existe)."""
    if set(usuario.roles) - {"CLIENTE"}:
        return
    fila = conn.execute("SELECT cliente_id::text AS c FROM pedidos.pedido WHERE pedido_id = %s", (pedido_id,)).fetchone()
    if not fila or fila["c"] != usuario.usuario_id:
        raise ErrorNegocio("NO_ENCONTRADO", "Pedido inexistente")


@router.get("/v1/pedidos/{pedido_id}", summary="API-PED-04 ConsultarPedido")
def consultar_pedido(pedido_id: str, usuario: Usuario = Depends(requiere_rol("CLIENTE", "ATENCION", "SUPERVISOR", "AUDITOR"))):
    with transaccion() as conn:
        _verificar_dueno(conn, pedido_id, usuario)
        return servicio.consultar(conn, pedido_id)


@router.get("/v1/pedidos/{pedido_id}/bitacora", summary="API-PED-10 BitacoraCliente (tiempo real, sin datos internos del personal)")
def bitacora_cliente(pedido_id: str, vista: str = Query("cliente", pattern="^(cliente|personal)$"),
                     usuario: Usuario = Depends(requiere_rol("CLIENTE", "ATENCION", "SUPERVISOR", "ENCARGADO", "AUDITOR"))):
    if vista == "personal" and not set(usuario.roles) & {"ATENCION", "SUPERVISOR", "ENCARGADO", "AUDITOR"}:
        raise ErrorNegocio("NO_AUTORIZADO", "La vista del personal requiere ATENCION, SUPERVISOR, ENCARGADO o AUDITOR")
    try:
        pedido_id = str(uuid.UUID(pedido_id))
    except ValueError as exc:
        raise ErrorNegocio("NO_ENCONTRADO", "Pedido inexistente") from exc
    with transaccion() as conn:
        _verificar_dueno(conn, pedido_id, usuario)
        return servicio.bitacora_cliente(conn, pedido_id, vista)


@router.post("/v1/pedidos/{pedido_id}/pago", status_code=202, summary="API-PED-03 IniciarPago (una sola llamada de cobro)")
def iniciar_pago(pedido_id: str, request: Request, idempotency_key: str = Header(...),
                 usuario: Usuario = Depends(requiere_rol("CLIENTE"))):
    with transaccion() as conn:
        _verificar_dueno(conn, pedido_id, usuario)
    return servicio.iniciar_pago(pedido_id, _clave(idempotency_key), request.state.correlacion_id)


@router.post("/internal/v1/eventos-pago", status_code=200, summary="Entrega de eventos de Pagos (bandeja de salida de M4)")
def recibir_evento_pago(evento: EventoPago, request: Request, servicio_origen: str = Depends(requiere_servicio)):
    if evento.estado == "EN_REVISION_FINANZAS":
        return {"desenlace": "SIN_CAMBIO"}           # la alerta la gestiona Finanzas; el pedido sigue PAGO_INCIERTO
    return servicio.procesar_resultado_pago(evento.model_dump(), request.state.correlacion_id, actor=servicio_origen)


class EventoAjuste(BaseModel):
    ajuste_id: str
    pedido_id: str
    estado: str
    monto: str | None = None


@router.post("/internal/v1/eventos-ajuste", status_code=200, summary="Resultado de un ajuste de cobro (M4 → M2)")
def recibir_evento_ajuste(evento: EventoAjuste, request: Request, servicio_origen: str = Depends(requiere_servicio)):
    return servicio.procesar_resultado_ajuste(evento.model_dump(), request.state.correlacion_id)
