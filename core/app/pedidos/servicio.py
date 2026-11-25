"""M2 · Pedidos (orquestador). Asignación de tienda (ADR-13), checkout, inicio de pago y desenlace del pago (R-D02)."""
import uuid
from decimal import Decimal

from psycopg import Connection
from psycopg.types.json import Jsonb

from ..db import transaccion
from ..errores import ErrorNegocio
from ..inventario import servicio as inventario
from . import bitacora
from .cliente_pagos import cliente_pagos

ESTADOS_SIN_DESENLACE = ("RESERVADO", "PAGO_SOLICITADO", "PAGO_INCIERTO", "NO_PAGADO", "ABANDONADO", "ESPERANDO_DECISION")


def _publicar(conn: Connection, tipo: str, agregado: str, corr, datos: dict):
    conn.execute("SELECT comun.publicar('M2', %s, %s, %s, %s)", (tipo, agregado, corr, Jsonb(datos)))


def _notificar(conn: Connection, destinatario: str, tipo: str, datos: dict, clave: str):
    """M6: un aviso por canal (aplicación y correo, Cliente 1-nov); UNIQUE(clave, canal) lo hace idempotente."""
    for canal in ("APP", "CORREO"):
        conn.execute(
            """INSERT INTO notificaciones.notificacion (destinatario, tipo, canal, datos, clave)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (clave, canal) DO NOTHING""",
            (destinatario, tipo, canal, Jsonb(datos), clave))


def asignar_tienda(conn: Connection, codigo_postal: str, lineas: list[dict]) -> dict:
    """ADR-13: filtro obligatorio (cobertura y disponibilidad de TODAS las líneas) + orden (cercanía, carga, tienda_id)."""
    candidatas = conn.execute(
        """SELECT z.tienda_id, z.distancia_km,
                  (SELECT count(*) FROM pedidos.pedido p WHERE p.tienda_id = z.tienda_id
                      AND p.estado IN ('PAGADO','EN_PREPARACION')) AS carga,
                  (SELECT count(*) FROM jsonb_to_recordset(%s) AS l(sku text, cantidad int)
                    WHERE COALESCE(inventario.disponible(z.tienda_id, l.sku), 0) >= l.cantidad) AS lineas_disponibles
             FROM pedidos.zona_cobertura z JOIN inventario.dark_store d USING (tienda_id)
            WHERE z.codigo_postal = %s AND z.activa AND d.activa
            ORDER BY (SELECT count(*) FROM jsonb_to_recordset(%s) AS l(sku text, cantidad int)
                       WHERE COALESCE(inventario.disponible(z.tienda_id, l.sku), 0) >= l.cantidad) = %s DESC,
                     z.distancia_km, carga, z.tienda_id""",
        (Jsonb(lineas), codigo_postal, Jsonb(lineas), len(lineas))).fetchall()
    if not candidatas:
        raise ErrorNegocio("SIN_COBERTURA", f"Ninguna Dark Store cubre el código postal {codigo_postal}")
    completas = [c for c in candidatas if c["lineas_disponibles"] == len(lineas)]
    elegida = completas[0] if completas else max(candidatas, key=lambda c: c["lineas_disponibles"])  # luego RF07
    return {"tienda_id": elegida["tienda_id"], "alternativas": [dict(c) for c in candidatas]}


def checkout(clave_k1: str, cliente_id: str, codigo_postal: str, lineas: list[dict], envio: Decimal,
             propina: Decimal, descuento: Decimal, corr: str) -> dict:
    """API-PED-01 IniciarCheckout. Una transacción: pedido + líneas + SolicitarReserva (todo o nada, RF04)."""
    with transaccion() as conn:
        previo = conn.execute("SELECT pedido_id FROM pedidos.pedido WHERE clave_checkout = %s", (clave_k1,)).fetchone()
        if previo:
            return consultar(conn, previo["pedido_id"])                 # reintento con la misma K1
        tienda = asignar_tienda(conn, codigo_postal, lineas)["tienda_id"]
        precios = {f["sku"]: f["precio"] for f in conn.execute(
            "SELECT sku, precio FROM catalogo.producto WHERE activo AND sku = ANY(%s)", ([l["sku"] for l in lineas],))}
        if len(precios) != len(lineas):
            raise ErrorNegocio("CANTIDAD_INVALIDA", "SKU inexistente, inactivo o repetido")
        subtotal = sum(precios[l["sku"]] * l["cantidad"] for l in lineas)
        pedido_id = conn.execute(
            """INSERT INTO pedidos.pedido (cliente_id, tienda_id, codigo_postal, estado, subtotal_mercancia, envio, propina,
                                           descuento_promocion, clave_checkout)
               VALUES (%s, %s, %s, 'EN_CHECKOUT', %s, %s, %s, %s, %s) RETURNING pedido_id""",
            (cliente_id, tienda, codigo_postal, subtotal, envio, propina, descuento, clave_k1)).fetchone()["pedido_id"]
        reservables = []
        for l in lineas:   # el precio queda fijado en el checkout (Cliente 1-nov, 21)
            linea_id = conn.execute(
                """INSERT INTO pedidos.linea_pedido (pedido_id, sku, cantidad_pedida, precio_unitario)
                   VALUES (%s, %s, %s, %s) RETURNING linea_id""",
                (pedido_id, l["sku"], l["cantidad"], precios[l["sku"]])).fetchone()["linea_id"]
            reservables.append({"linea_id": str(linea_id), "sku": l["sku"], "cantidad": l["cantidad"]})
        _publicar(conn, "PedidoCreado", str(pedido_id), corr, {"pedido_id": str(pedido_id), "tienda_id": tienda})
        r = inventario.solicitar_reserva(conn, pedido_id, tienda, reservables, f"{clave_k1}#reserva", f"cliente:{cliente_id}", corr)
        if r["resultado"] == "ACEPTADA":
            conn.execute("UPDATE pedidos.pedido SET estado = 'RESERVADO', actualizado_en = clock_timestamp() WHERE pedido_id = %s", (pedido_id,))
            conn.execute("UPDATE pedidos.linea_pedido SET estado = 'RESERVADA' WHERE pedido_id = %s", (pedido_id,))
        else:   # AGOTADO o SKU_BLOQUEADO: no se reservó nada; decide el cliente (RF07)
            conn.execute("UPDATE pedidos.pedido SET estado = 'ESPERANDO_DECISION' WHERE pedido_id = %s", (pedido_id,))
        resumen = consultar(conn, pedido_id)
        resumen["reserva"] = r
        return resumen


def consultar(conn: Connection, pedido_id) -> dict:
    p = conn.execute(
        """SELECT pedido_id, tienda_id, estado, subtotal_mercancia, envio, propina, descuento_promocion, total, hora_confirmacion
             FROM pedidos.pedido WHERE pedido_id = %s""", (pedido_id,)).fetchone()
    if not p:
        raise ErrorNegocio("NO_ENCONTRADO", "Pedido inexistente")
    p = dict(p)
    p["lineas"] = [dict(x) for x in conn.execute(
        """SELECT linea_id, sku, cantidad_pedida, precio_unitario, cantidad_surtida, cantidad_cancelada, estado
             FROM pedidos.linea_pedido WHERE pedido_id = %s ORDER BY sku""", (pedido_id,))]
    return p


def iniciar_pago(pedido_id: str, clave_k2: str, corr: str) -> dict:
    """API-PED-03. INV-15: solo con reserva Activa y vigente. Después, UNA llamada a Pagos (sin reintento tras timeout)."""
    with transaccion() as conn:
        p = conn.execute("SELECT estado, total FROM pedidos.pedido WHERE pedido_id = %s FOR UPDATE", (pedido_id,)).fetchone()
        if not p:
            raise ErrorNegocio("NO_ENCONTRADO", "Pedido inexistente")
        if p["estado"] in ("PAGO_SOLICITADO", "PAGO_INCIERTO"):
            raise ErrorNegocio("PAGO_EN_CURSO", "Ya existe un cobro en curso; no se reintenta (ADR-08)")
        if p["estado"] != "RESERVADO":
            raise ErrorNegocio("ESTADO_INVALIDO", f"El pedido está en {p['estado']}")
        r = inventario.reserva_del_pedido(conn, pedido_id)
        if not r or r["estado"] != "ACTIVA" or not r["vigente"]:
            raise ErrorNegocio("RESERVA_VENCIDA", "La reserva de 300 s venció; no se inicia el cobro (CA-04)")
        conn.execute("UPDATE pedidos.pedido SET estado = 'PAGO_SOLICITADO', actualizado_en = clock_timestamp() WHERE pedido_id = %s",
                     (pedido_id,))
        total = str(p["total"])
        _publicar(conn, "PagoSolicitado", str(pedido_id), corr, {"pedido_id": str(pedido_id), "monto": total})
    respuesta = cliente_pagos.solicitar_cobro(str(pedido_id), total, clave_k2, corr)
    if respuesta.get("estado") in ("APROBADO", "RECHAZADO", "INCIERTO") and respuesta.get("pago_id"):
        procesar_resultado_pago({**respuesta, "pedido_id": str(pedido_id)}, corr)   # también llegará por evento: idempotente
    elif respuesta.get("estado") == "INCIERTO":
        with transaccion() as conn:
            cambio = conn.execute("UPDATE pedidos.pedido SET estado = 'PAGO_INCIERTO' WHERE pedido_id = %s AND estado = 'PAGO_SOLICITADO' "
                                  "RETURNING pedido_id", (pedido_id,)).fetchone()
            if cambio:
                _publicar(conn, "PagoResultado", str(pedido_id), corr,
                          {"pedido_id": str(pedido_id), "desenlace": "PAGO_INCIERTO", "motivo": "SIN_RESPUESTA_DE_PAGOS"})
    with transaccion() as conn:
        return consultar(conn, pedido_id)


def procesar_resultado_pago(evento: dict, corr: str | None, actor: str = "M2", reparacion: bool = False) -> dict:
    """Desenlace del pago (§20.3 de S2). Idempotente por notificacion_id (N1): la misma N1 nunca produce dos efectos."""
    pedido_id, estado = evento["pedido_id"], evento["estado"]
    n1 = evento.get("notificacion_id") or f"{evento.get('pago_id')}:{estado}"
    with transaccion() as conn:
        previo = conn.execute("SELECT comun.reclamar_clave(%s, 'ResultadoPago', %s) AS r",
                              (f"evt:{n1}", f"{pedido_id}|{estado}")).fetchone()["r"]
        if previo is not None:
            return previo
        p = conn.execute("SELECT estado, cliente_id, subtotal_mercancia FROM pedidos.pedido WHERE pedido_id = %s FOR UPDATE",
                         (pedido_id,)).fetchone()
        reserva = inventario.reserva_del_pedido(conn, pedido_id)
        cambio = True
        if estado == "INCIERTO":
            cambio = conn.execute("UPDATE pedidos.pedido SET estado = 'PAGO_INCIERTO' WHERE pedido_id = %s "
                                  "AND estado IN ('PAGO_SOLICITADO','RESERVADO') RETURNING pedido_id", (pedido_id,)).fetchone() is not None
            resultado = {"desenlace": "PAGO_INCIERTO"}
        elif estado == "RECHAZADO":
            if reserva and reserva["estado"] == "ACTIVA":
                inventario.liberar_reserva(conn, reserva["reserva_id"], "PAGO_RECHAZADO", f"lib:{n1}", actor, corr)
            cambio = conn.execute("UPDATE pedidos.pedido SET estado = 'NO_PAGADO' WHERE pedido_id = %s "
                                  "AND estado IN ('PAGO_SOLICITADO','PAGO_INCIERTO','RESERVADO') RETURNING pedido_id",
                                  (pedido_id,)).fetchone() is not None
            _notificar(conn, str(p["cliente_id"]), "PAGO_RECHAZADO", {"pedido_id": pedido_id}, f"rechazo:{pedido_id}")
            resultado = {"desenlace": "NO_PAGADO"}
        else:  # APROBADO: decide R-D02 dentro de M3
            conf = inventario.confirmar_reserva(conn, reserva["reserva_id"], n1, actor, corr) if reserva else {"resultado": "RESERVA_NO_ACTIVA"}
            if conf["resultado"] == "CONFIRMADA" and p["estado"] in ("PAGO_SOLICITADO", "PAGO_INCIERTO", "RESERVADO"):
                conn.execute("""UPDATE pedidos.pedido SET estado = 'PAGADO', hora_confirmacion = clock_timestamp(),
                                actualizado_en = clock_timestamp() WHERE pedido_id = %s""", (pedido_id,))
                conn.execute("UPDATE pedidos.linea_pedido SET estado = 'COMPROMETIDA' WHERE pedido_id = %s", (pedido_id,))
                _notificar(conn, str(p["cliente_id"]), "PEDIDO_CONFIRMADO", {"pedido_id": pedido_id}, f"confirmado:{pedido_id}")
                resultado = {"desenlace": "PAGADO"}
            else:  # pago tardío: no se revive la reserva; se concilia y reembolsa la mercancía (Cliente 1-nov, 12-13)
                for l in conn.execute("SELECT linea_id, cantidad_pedida FROM pedidos.linea_pedido WHERE pedido_id = %s", (pedido_id,)).fetchall():
                    conn.execute(
                        """INSERT INTO pedidos.cancelacion_linea (linea_id, cantidad, causa, clave, actor)
                           VALUES (%s, %s, 'PAGO_SIN_EXISTENCIA', %s, %s) ON CONFLICT (clave) DO NOTHING""",
                        (l["linea_id"], l["cantidad_pedida"], f"pse:{l['linea_id']}", actor))
                conn.execute("UPDATE pedidos.linea_pedido SET estado = 'CANCELADA_PAGO_SIN_EXISTENCIA' WHERE pedido_id = %s", (pedido_id,))
                conn.execute("UPDATE pedidos.pedido SET estado = 'PAGO_SIN_EXISTENCIA', actualizado_en = clock_timestamp() WHERE pedido_id = %s",
                             (pedido_id,))
                # El reembolso sale por la bandeja de salida: el worker lo entrega a Pagos con K4 determinista
                _publicar(conn, "SolicitudAjusteCobro", pedido_id, corr,
                          {"pedido_id": pedido_id, "componente": "MERCANCIA", "tipo": "TOTAL",
                           "monto": str(p["subtotal_mercancia"]), "motivo": "PAGO_SIN_EXISTENCIA", "clave": f"{pedido_id}#PSE"})
                _notificar(conn, str(p["cliente_id"]), "CANCELACION_TOTAL", {"pedido_id": pedido_id, "causa": "PAGO_TARDIO"},
                           f"cancelacion:{pedido_id}")
                resultado = {"desenlace": "PAGO_SIN_EXISTENCIA", "inventario": conf}
        if cambio:   # para la bitácora del cliente (ADR-29): misma transacción que el cambio
            _publicar(conn, "PagoResultado", pedido_id, corr, {"pedido_id": pedido_id, "desenlace": resultado["desenlace"]})
        if reparacion:
            _publicar(conn, "REPARACION_APLICADA", pedido_id, corr,
                      {"pedido_id": pedido_id, "pago_id": evento.get("pago_id"), "notificacion_id": n1,
                       "estado_pedido_antes": p["estado"], "accion": "REPROCESAR_RESULTADO_PAGO", "resultado": resultado})
        conn.execute("SELECT comun.guardar_resultado(%s, %s)", (f"evt:{n1}", Jsonb(resultado)))
        return resultado


def bitacora_cliente(conn: Connection, pedido_id: str, vista: str = "cliente") -> dict:
    """API-PED-10 · Bitácora en tiempo real (lee la bandeja de salida). Vista cliente: sin datos internos.
    Vista personal: además quién operó (nombre, rol e id) y la correlación."""
    pedido = conn.execute(
        """SELECT pedido_id, cliente_id, tienda_id, estado, subtotal_mercancia, envio, propina, descuento_promocion, total
             FROM pedidos.pedido WHERE pedido_id = %s""", (pedido_id,)).fetchone()
    if not pedido:
        raise ErrorNegocio("NO_ENCONTRADO", "Pedido inexistente")
    lineas = conn.execute(
        """SELECT l.sku, p.descripcion, l.precio_unitario, l.cantidad_pedida, l.cantidad_surtida, l.cantidad_cancelada, l.estado
             FROM pedidos.linea_pedido l LEFT JOIN catalogo.producto p USING (sku)
            WHERE l.pedido_id = %s ORDER BY l.sku""", (pedido_id,)).fetchall()
    reserva = conn.execute(
        """SELECT estado, expira_en FROM inventario.reserva WHERE pedido_id = %s ORDER BY creada_en DESC LIMIT 1""",
        (pedido_id,)).fetchone()
    eventos = conn.execute(
        """SELECT tipo, creado_en, datos, correlacion_id FROM comun.outbox WHERE datos->>'pedido_id' = %s ORDER BY creado_en""",
        (str(pedido_id),)).fetchall()
    avisos = conn.execute(
        """SELECT tipo, datos, creada_en FROM notificaciones.notificacion
            WHERE datos->>'pedido_id' = %s AND canal = 'APP' ORDER BY creada_en""", (str(pedido_id),)).fetchall()
    ahora = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    personas = {}
    if vista == "personal":
        from ..operacion.servicio import nombres
        ids = [pedido["cliente_id"]] + [e["datos"].get(k) for e in eventos for k in ("picker_id", "supervisor_id") if e["datos"]]
        personas = nombres(conn, ids)
    return bitacora.armar(pedido, lineas, reserva, eventos, avisos, ahora, vista, personas)


def procesar_resultado_ajuste(evento: dict, corr: str | None) -> dict:
    """AjusteCobroAplicado → Reembolsado + aviso REEMBOLSO_CONFIRMADO. Otros estados no cambian el pedido (Finanzas los resuelve)."""
    if evento["estado"] != "APLICADO":
        return {"desenlace": "SIN_CAMBIO", "estado_ajuste": evento["estado"]}
    with transaccion() as conn:
        p = conn.execute(
            """UPDATE pedidos.pedido SET estado = CASE WHEN estado IN ('PAGO_SIN_EXISTENCIA','CANCELADO_POR_CLIENTE')
                                                       THEN 'REEMBOLSADO' ELSE estado END,
                                        actualizado_en = clock_timestamp()
                WHERE pedido_id = %s RETURNING cliente_id, estado""", (evento["pedido_id"],)).fetchone()
        if p:
            _notificar(conn, str(p["cliente_id"]), "REEMBOLSO_CONFIRMADO",
                       {"pedido_id": evento["pedido_id"], "monto": evento.get("monto")}, f"reembolso:{evento['ajuste_id']}")
        return {"desenlace": p["estado"] if p else "PEDIDO_DESCONOCIDO"}


def reparar(desde: str) -> int:
    """API-PED-09 · Ciclo de reparación (ADR-12): pagos finales en M4 sin desenlace en el pedido."""
    reparados = 0
    # Si no hay pedidos sin desenlace en la ventana, no hay nada que reparar: no se consulta a Pagos
    # (evita despertar al servicio cuando escala a cero, p. ej. en Azure Container Apps).
    with transaccion() as conn:
        pendiente = conn.execute(
            "SELECT 1 FROM pedidos.pedido WHERE estado = ANY(%s) AND actualizado_en >= %s LIMIT 1",
            (list(ESTADOS_SIN_DESENLACE), desde)).fetchone()
    if not pendiente:
        return 0
    for pago in cliente_pagos.pagos_finales(desde):
        with transaccion() as conn:
            fila = conn.execute("SELECT estado FROM pedidos.pedido WHERE pedido_id = %s", (pago["pedido_id"],)).fetchone()
        if fila and fila["estado"] in ESTADOS_SIN_DESENLACE:
            procesar_resultado_pago(pago, corr=None, actor="REPARACION", reparacion=True)
            reparados += 1
    return reparados


def nuevo_id() -> str:
    return str(uuid.uuid4())
