"""M5 · Operación (C3): preparación, surtido, faltantes validados por supervisor, conteo físico y ajuste autorizado.
Las reglas viven en las funciones PL/pgSQL de db/core/005_operacion_c3.sql (una transacción y un viaje por operación)."""
import uuid

import psycopg
from psycopg.types.json import Jsonb

from ..db import transaccion
from ..errores import ErrorNegocio


def _uuid(valor: str, campo: str = "usuario") -> str:
    try:
        return str(uuid.UUID(str(valor)))
    except ValueError as exc:
        raise ErrorNegocio("IDENTIDAD_INVALIDA", f"{campo} debe ser un UUID") from exc


def _ejecutar(sql: str, params: tuple) -> dict:
    """Ejecuta una función con efecto y traduce sus errores de negocio (RAISE … P0001) a problem+json."""
    try:
        with transaccion() as conn:
            r = conn.execute(sql, params).fetchone()["r"]
    except psycopg.errors.RaiseException as exc:
        raise ErrorNegocio(exc.diag.message_primary or "ERROR", exc.diag.message_detail or "") from exc
    if r.get("resultado") == "ERROR":
        raise ErrorNegocio(r.get("error", "ERROR"), "")
    return r


def iniciar_preparacion(pedido_id: str, actor: str, corr: str) -> dict:
    return _ejecutar("SELECT operacion.iniciar_preparacion(%s, %s, %s) AS r", (_uuid(pedido_id, "pedido_id"), _uuid(actor), corr))


def surtir(linea_id: str, cantidad: int, picker: str, clave: str, corr: str) -> dict:
    return _ejecutar("SELECT operacion.surtir_linea(%s, %s, %s, %s, %s) AS r",
                     (_uuid(linea_id, "linea_id"), cantidad, _uuid(picker), clave, corr))


def reportar_faltante(linea_id: str, encontradas: int, faltantes: int, motivo: str, descripcion: str | None,
                      picker: str, clave: str, corr: str) -> dict:
    return _ejecutar("SELECT operacion.reportar_faltante(%s, %s, %s, %s, %s, %s, %s, %s) AS r",
                     (_uuid(linea_id, "linea_id"), encontradas, faltantes, motivo, descripcion, _uuid(picker), clave, corr))


def validar_faltante(faltante_id: str, supervisor: str, valido: bool, evidencia: str, clave: str, corr: str) -> dict:
    return _ejecutar("SELECT operacion.validar_faltante(%s, %s, %s, %s, %s, %s) AS r",
                     (_uuid(faltante_id, "faltante_id"), _uuid(supervisor), valido, evidencia, clave, corr))


def registrar_conteo(tienda_id: int, sku: str, cantidad_fisica: int, responsable: str, clave: str, corr: str) -> dict:
    return _ejecutar("SELECT operacion.registrar_conteo(%s::smallint, %s, %s, %s, %s, %s) AS r",
                     (tienda_id, sku, cantidad_fisica, _uuid(responsable), clave, corr))


def autorizar_ajuste(ajuste_id: str, autorizador: str, aprobar: bool, evidencia: str, clave: str, corr: str) -> dict:
    return _ejecutar("SELECT operacion.autorizar_ajuste(%s, %s, %s, %s, %s, %s) AS r",
                     (_uuid(ajuste_id, "ajuste_id"), _uuid(autorizador), aprobar, evidencia, clave, corr))


def historial_existencia(tienda_id: int, sku: str, limite: int) -> dict:
    """C3 · quién/qué cambió el inventario, cuándo, por qué y a qué pedido afectó; con la reconstrucción del saldo."""
    with transaccion() as conn:
        if not conn.execute("SELECT 1 FROM inventario.existencia WHERE tienda_id = %s AND sku = %s", (tienda_id, sku)).fetchone():
            raise ErrorNegocio("NO_ENCONTRADO", "Existencia inexistente")
        movimientos = conn.execute(
            """SELECT m.movimiento_id, m.registrado_en, m.tipo, m.saldo, m.delta, m.saldo_antes, m.saldo_despues, m.motivo,
                      m.actor, m.doc_tipo, m.doc_id, m.correlacion_id,
                      COALESCE(CASE WHEN m.doc_tipo = 'PEDIDO' THEN m.doc_id::uuid END, r.pedido_id) AS pedido_id
                 FROM inventario.movimiento m
                 LEFT JOIN inventario.reserva r ON m.doc_tipo = 'RESERVA' AND r.reserva_id::text = m.doc_id
                WHERE m.tienda_id = %s AND m.sku = %s ORDER BY m.movimiento_id DESC LIMIT %s""",
            (tienda_id, sku, limite)).fetchall()
        return {
            "tienda_id": tienda_id, "sku": sku,
            "existencia": conn.execute(
                """SELECT vendible, comprometido, inventario.reservas_vigentes(tienda_id, sku, clock_timestamp()) AS reservado,
                          inventario.disponible(tienda_id, sku) AS disponible, estado
                     FROM inventario.existencia WHERE tienda_id = %s AND sku = %s""", (tienda_id, sku)).fetchone(),
            "reconstruccion": conn.execute("SELECT * FROM inventario.reconstruir_saldos(%s::smallint, %s)", (tienda_id, sku)).fetchall(),
            "discrepancias": conn.execute(
                """SELECT discrepancia_id, origen, cantidad_logica, cantidad_fisica, diferencia, estado, creada_en, resuelta_en,
                          resuelta_por_movimiento_id FROM inventario.discrepancia
                    WHERE tienda_id = %s AND sku = %s ORDER BY creada_en DESC""", (tienda_id, sku)).fetchall(),
            "ajustes": conn.execute(
                """SELECT ajuste_inv_id AS ajuste_id, tipo, cantidad, motivo, solicitante_id, autorizador_id, estado, movimiento_id,
                          creado_en FROM operacion.ajuste_inventario WHERE tienda_id = %s AND sku = %s ORDER BY creado_en DESC""",
                (tienda_id, sku)).fetchall(),
            "conteos": conn.execute(
                """SELECT tarea_id, tipo, disparador, nivel_riesgo, responsable_id, cantidad_fisica, estado, inicio, fin, duracion
                     FROM operacion.tarea_conteo WHERE tienda_id = %s AND sku = %s ORDER BY creada_en DESC""",
                (tienda_id, sku)).fetchall(),
            "movimientos": movimientos,
        }


def historial_pedido(conn, pedido_id: str) -> dict:
    """C3 · cómo impactó el inventario al pedido: faltantes, cancelaciones, movimientos y eventos de la bitácora."""
    reservas = [str(r["reserva_id"]) for r in conn.execute(
        "SELECT reserva_id FROM inventario.reserva WHERE pedido_id = %s", (pedido_id,)).fetchall()]
    return {
        "pedido_id": pedido_id,
        "faltantes": conn.execute(
            """SELECT f.faltante_id, l.sku, f.encontradas, f.faltantes, f.motivo, f.picker_id, f.estado, f.supervisor_id,
                      f.evidencia, f.creado_en, f.validado_en, f.discrepancia_id
                 FROM operacion.reporte_faltante f JOIN pedidos.linea_pedido l USING (linea_id)
                WHERE l.pedido_id = %s ORDER BY f.creado_en""", (pedido_id,)).fetchall(),
        "cancelaciones": conn.execute(
            """SELECT c.cancelacion_id, l.sku, c.cantidad, c.causa, c.discrepancia_id, c.actor, c.registrada_en
                 FROM pedidos.cancelacion_linea c JOIN pedidos.linea_pedido l USING (linea_id)
                WHERE l.pedido_id = %s ORDER BY c.registrada_en""", (pedido_id,)).fetchall(),
        "movimientos": conn.execute(
            """SELECT movimiento_id, registrado_en, tienda_id, sku, tipo, saldo, delta, saldo_antes, saldo_despues, motivo, actor
                 FROM inventario.movimiento
                WHERE (doc_tipo = 'PEDIDO' AND doc_id = %s) OR (doc_tipo = 'RESERVA' AND doc_id = ANY(%s))
                ORDER BY movimiento_id""", (pedido_id, reservas)).fetchall(),
        "eventos": conn.execute(
            """SELECT tipo, hora_servidor, actor, correlacion_id, datos FROM trazabilidad.evento_bitacora
                WHERE pedido_id = %s ORDER BY hora_servidor""", (pedido_id,)).fetchall(),
    }
