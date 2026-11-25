"""Bitácora del pedido para el CLIENTE (ADR-29 propuesto): línea de tiempo en lenguaje claro y en tiempo real.

- Lee la bandeja de salida (comun.outbox), que se escribe en la MISMA transacción que cada cambio de negocio:
  el cliente ve el movimiento en cuanto ocurre, sin esperar a que el worker lo copie a trazabilidad.
- No expone datos internos: ni identificadores del personal, ni evidencia del supervisor, ni correlación.
- Módulo puro (sin FastAPI ni base de datos) para poder probarlo de forma aislada.
"""
from datetime import datetime
from decimal import Decimal

ESTADO_TEXTO = {
    "EN_CHECKOUT": "Creando tu pedido",
    "ESPERANDO_DECISION": "No se pudieron apartar los productos",
    "RESERVADO": "Productos apartados: falta pagar",
    "RESERVA_VENCIDA": "La reserva venció sin pago",
    "PAGO_SOLICITADO": "Pago en proceso",
    "PAGO_INCIERTO": "Pago en revisión",
    "PAGADO": "Pagado: esperando a la tienda",
    "EN_PREPARACION": "La tienda está preparando tu pedido",
    "COMPLETO": "Pedido completo",
    "PARCIAL": "Pedido entregado en parte",
    "CANCELADO_TOTAL": "Pedido cancelado por falta de producto",
    "NO_PAGADO": "Pedido sin pago",
    "ABANDONADO": "Pedido abandonado",
    "PAGO_SIN_EXISTENCIA": "Pago tardío: pedido cancelado",
    "REEMBOLSADO": "Reembolsado",
    "CANCELADO_POR_CLIENTE": "Cancelado por ti",
}
FINALES = {"COMPLETO", "NO_PAGADO", "REEMBOLSADO", "CANCELADO_POR_CLIENTE", "ABANDONADO", "ESPERANDO_DECISION"}
ESPERAN_REEMBOLSO = {"PARCIAL", "CANCELADO_TOTAL", "PAGO_SIN_EXISTENCIA"}
COMPONENTES = {"ENVIO": "el envío", "PROPINA": "la propina", "PROMOCION": "la promoción"}


def _dinero(x) -> str:
    return f"${Decimal(str(x)):.2f}"


def _texto(tipo: str, d: dict, nombre) -> tuple[str, str] | None:
    """Traduce un evento a (título, detalle) para el cliente. None = evento interno que el cliente no ve."""
    if tipo == "PedidoCreado":
        return "Pedido creado", f"Lo atenderá la Dark Store {d.get('tienda_id')}."
    if tipo == "ReservaCreada":
        return "Productos apartados por 5 minutos", "Paga antes de que termine el tiempo; después se liberan."
    if tipo == "SolicitudReservaRechazada":
        skus = ", ".join(nombre(x.get("sku")) for x in d.get("lineas", []) if x.get("sku"))
        return "No se pudieron apartar los productos", (f"Sin existencia suficiente o en verificación: {skus}. " if skus else "") + \
            "No se apartó nada."
    if tipo == "ReservaExpirada":
        return "La reserva venció", "Pasaron los 5 minutos sin pago: los productos se liberaron y no hubo cobro."
    if tipo in ("ReservaLiberada", "ReservaLiberadaPorAgotamiento"):
        return "Reserva liberada", "Los productos apartados se liberaron; no hubo cobro."
    if tipo == "PagoSolicitado":
        return "Pago en proceso", f"Se solicitó el cobro de {_dinero(d.get('monto', 0))}."
    if tipo == "PagoResultado":
        return {
            "PAGADO": ("Pago aprobado", "Tu pedido quedó confirmado."),
            "NO_PAGADO": ("Pago rechazado", "No se hizo ningún cargo y los productos se liberaron."),
            "PAGO_INCIERTO": ("Pago en revisión", "No hubo respuesta de la pasarela. No se te cobrará dos veces; te avisaremos."),
            "PAGO_SIN_EXISTENCIA": ("Pago recibido después del plazo",
                                    "La reserva ya había vencido: se canceló el pedido y se solicitó el reembolso de la mercancía."),
        }.get(d.get("desenlace"), ("Resultado del pago", str(d.get("desenlace", ""))))
    if tipo == "ReservaConfirmada":
        return "Productos asegurados", "Las unidades quedaron comprometidas para tu pedido."
    if tipo == "PreparacionIniciada":
        return "En preparación", "La tienda empezó a surtir tu pedido."
    if tipo == "LineaSurtida":
        return "Producto listo", f"{d.get('cantidad')} × {nombre(d.get('sku'))}"
    if tipo == "FaltanteReportado":
        return "Verificando un producto", f"La tienda está confirmando la existencia de {nombre(d.get('sku'))}."
    if tipo == "FaltanteValidado":
        return "Producto no disponible", (f"No había {d.get('cantidad_cancelada')} × {nombre(d.get('sku'))}: se canceló esa parte "
                                          f"y se solicitará el reembolso de {_dinero(d.get('reembolso', 0))}.")
    if tipo == "FaltanteNoValidado":
        return "Producto encontrado", f"{nombre(d.get('sku'))} sí está en la tienda; seguimos preparando."
    if tipo == "SolicitudAjusteCobro":
        if d.get("componente") == "MERCANCIA":
            return "Reembolso solicitado", f"{_dinero(d.get('monto', 0))} de mercancía."
        return "Revisión de Finanzas", f"Finanzas revisará {COMPONENTES.get(d.get('componente'), 'un cargo')} ({_dinero(d.get('monto', 0))})."
    if tipo == "PedidoPreparado":
        return {
            "COMPLETO": ("Pedido completo", "Se surtieron todos los productos."),
            "PARCIAL": ("Pedido entregado en parte", f"Surtidas {d.get('unidades_surtidas')} unidades; canceladas {d.get('unidades_canceladas')}."),
            "CANCELADO_TOTAL": ("Pedido cancelado", "No hubo existencia de ningún producto."),
        }.get(d.get("estado"), ("Pedido preparado", ""))
    if tipo == "REPARACION_APLICADA":
        return "Pago verificado", "Confirmamos el resultado de tu pago con una verificación automática."
    return None


# Varios eventos nacen en la misma transacción (p. ej. faltante validado + solicitud de reembolso) con microsegundos de
# diferencia; dentro de esa ráfaga se ordenan como los lee una persona: causa antes que consecuencia.
RANGO = {"PedidoCreado": 0, "ReservaCreada": 1, "SolicitudReservaRechazada": 1, "PagoSolicitado": 2, "PagoResultado": 3,
         "PEDIDO_CONFIRMADO": 3, "PAGO_RECHAZADO": 3, "ReservaConfirmada": 4, "ReservaLiberada": 4, "ReservaExpirada": 4,
         "ReservaLiberadaPorAgotamiento": 4, "PreparacionIniciada": 5, "FaltanteValidado": 7, "FaltanteNoValidado": 7,
         "SolicitudAjusteCobro": 8, "PedidoPreparado": 9, "REEMBOLSO_CONFIRMADO": 10}
RAFAGA_S = 1.0


def _ordenar(items: list[dict]) -> list[dict]:
    items = sorted(items, key=lambda x: x["hora"])
    salida, grupo = [], []
    for it in items:
        if grupo and (it["hora"] - grupo[0]["hora"]).total_seconds() > RAFAGA_S:
            salida += sorted(grupo, key=lambda x: RANGO.get(x["tipo"], 6))
            grupo = []
        grupo.append(it)
    return salida + sorted(grupo, key=lambda x: RANGO.get(x["tipo"], 6))


def _actor(tipo: str, d: dict, cliente_id) -> tuple[str, str | None]:
    """Quién originó el evento: (rol, id). El id solo se muestra en la vista del personal."""
    if tipo in ("PedidoCreado", "ReservaCreada", "SolicitudReservaRechazada", "PagoSolicitado"):
        return "Cliente", str(cliente_id)
    if tipo in ("PreparacionIniciada", "LineaSurtida", "FaltanteReportado"):
        return "Picker", d.get("picker_id")
    if tipo in ("FaltanteValidado", "FaltanteNoValidado"):
        return "Supervisor", d.get("supervisor_id")
    if tipo in ("PagoResultado", "ReservaConfirmada", "ReservaLiberada", "REEMBOLSO_CONFIRMADO", "PEDIDO_CONFIRMADO", "PAGO_RECHAZADO"):
        return "Pagos", None
    return "Sistema", None


POR_CLIENTE = {"Cliente": "Tú", "Picker": "La tienda", "Supervisor": "La tienda", "Pagos": "Pagos", "Sistema": "Sistema"}


def _por(rol: str, actor_id, vista: str, personas: dict) -> dict:
    if vista != "personal":
        return {"por": POR_CLIENTE.get(rol, rol)}
    p = personas.get(str(actor_id)) if actor_id else None
    nombre = p["nombre"] if p else (f"{str(actor_id)[:8]}" if actor_id else None)
    return {"por": f"{rol} · {nombre}" if nombre else rol, "rol": rol, "actor_id": str(actor_id) if actor_id else None}


def armar(pedido: dict, lineas: list[dict], reserva: dict | None, eventos: list[dict], avisos: list[dict],
          ahora: datetime, vista: str = "cliente", personas: dict | None = None) -> dict:
    """pedido: fila de pedidos.pedido; lineas: con descripcion; reserva: la más reciente (o None);
    eventos: filas de comun.outbox (tipo, creado_en, datos) del pedido; avisos: notificaciones APP del pedido."""
    nombres = {l["sku"]: l.get("descripcion") or l["sku"] for l in lineas}
    nombre = lambda sku: nombres.get(sku, sku or "un producto")
    linea_de_tiempo = []
    tipos = set()
    personas = personas or {}
    cliente_id = pedido.get("cliente_id")
    for e in eventos:
        d = e["datos"] or {}
        t = _texto(e["tipo"], d, nombre)
        tipos.add(e["tipo"])
        if t:
            item = {"hora": e["creado_en"], "tipo": e["tipo"], "titulo": t[0], "detalle": t[1],
                    **_por(*_actor(e["tipo"], d, cliente_id), vista, personas)}
            if vista == "personal":
                item["correlacion_id"] = str(e["correlacion_id"]) if e.get("correlacion_id") else None
                if e["tipo"] == "FaltanteReportado":
                    item["detalle"] += f" Motivo: {d.get('motivo')}; faltan {d.get('faltantes')}."
            linea_de_tiempo.append(item)

    reembolso_confirmado = False
    for a in avisos:
        d = a["datos"] or {}
        if a["tipo"] == "REEMBOLSO_CONFIRMADO":
            reembolso_confirmado = True
            monto = f" por {_dinero(d['monto'])}" if d.get("monto") else ""
            linea_de_tiempo.append({"hora": a["creada_en"], "tipo": "REEMBOLSO_CONFIRMADO", "titulo": "Reembolso confirmado",
                                    "detalle": f"La pasarela aplicó tu reembolso{monto}.", **_por("Pagos", None, vista, personas)})
        elif "PagoResultado" not in tipos and a["tipo"] in ("PEDIDO_CONFIRMADO", "PAGO_RECHAZADO"):
            # Pedidos anteriores a la publicación de PagoResultado: se usa el aviso para no perder el dato
            ok = a["tipo"] == "PEDIDO_CONFIRMADO"
            linea_de_tiempo.append({"hora": a["creada_en"], "tipo": a["tipo"], "titulo": "Pago aprobado" if ok else "Pago rechazado",
                                    "detalle": "Tu pedido quedó confirmado." if ok else "No se hizo ningún cargo.",
                                    **_por("Pagos", None, vista, personas)})

    estado = pedido["estado"]
    reserva_info = None
    if reserva:
        vigente = reserva["estado"] == "ACTIVA" and ahora < reserva["expira_en"]
        reserva_info = {"estado": reserva["estado"], "expira_en": reserva["expira_en"], "vigente": vigente,
                        "segundos_restantes": max(0, int((reserva["expira_en"] - ahora).total_seconds())) if vigente else 0}
        # La reserva vence por reloj (inventario.disponible ya la ignora); el job solo lo registra después.
        if reserva["estado"] == "ACTIVA" and not vigente:
            if estado == "RESERVADO":
                estado = "RESERVA_VENCIDA"
            if "ReservaExpirada" not in tipos:
                linea_de_tiempo.append({"hora": reserva["expira_en"], "tipo": "ReservaVencida", "titulo": "La reserva venció",
                                        "detalle": "Pasaron los 5 minutos sin pago: los productos se liberaron y no hubo cobro.",
                                        **_por("Sistema", None, vista, personas)})

    linea_de_tiempo = _ordenar(linea_de_tiempo)
    if estado in FINALES or estado == "RESERVA_VENCIDA":   # una reserva vencida ya no cambia para el cliente
        en_curso = False
    elif estado in ESPERAN_REEMBOLSO:
        en_curso = not reembolso_confirmado
    else:
        en_curso = True
    return {
        "pedido_id": str(pedido["pedido_id"]), "tienda_id": pedido["tienda_id"],
        "estado": estado, "estado_texto": ESTADO_TEXTO.get(estado, estado),
        "subtotal_mercancia": pedido["subtotal_mercancia"], "envio": pedido["envio"], "propina": pedido["propina"],
        "descuento_promocion": pedido["descuento_promocion"], "total": pedido["total"],
        "lineas": [{"sku": l["sku"], "descripcion": nombre(l["sku"]), "precio_unitario": l["precio_unitario"],
                    "pedidas": l["cantidad_pedida"], "surtidas": l["cantidad_surtida"], "canceladas": l["cantidad_cancelada"],
                    "estado": l["estado"]} for l in lineas],
        "reserva": reserva_info,
        "eventos": linea_de_tiempo,
        "reembolso_confirmado": reembolso_confirmado,
        "en_curso": en_curso,
        "vista": vista,
        "hora_servidor": ahora,
    }
