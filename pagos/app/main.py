"""ExpressYa · Servicio de Pagos y Conciliación (M4) · FastAPI + base de datos propia (ADR-17).
Una sola llamada de cobro por K2; un timeout deja el pago INCIERTO y nunca se reenvía (ADR-05, ADR-08)."""
import hashlib
import hmac
import json
from contextlib import contextmanager
from decimal import Decimal

import jwt
from psycopg import errors as pg_errores
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import pasarela_simulada as pasarela
from .config import ajustes

pool = ConnectionPool(ajustes.database_url, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=False)
app = FastAPI(title="ExpressYa Pagos", version="0.3.0")


@contextmanager
def tx():
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


@app.on_event("startup")
def abrir():
    pool.open(wait=True)


def servicio(x_servicio_token: str = Header(default="")):
    if not hmac.compare_digest(x_servicio_token, ajustes.token_servicio):
        raise HTTPException(403, "Token de servicio inválido")


def finanzas(authorization: str = Header(default="")) -> str:
    try:
        datos = jwt.decode(authorization.removeprefix("Bearer "), ajustes.jwt_secreto, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, str(e)) from e
    if "FINANZAS" not in datos.get("roles", []):
        raise HTTPException(403, "Se requiere el rol FINANZAS")
    return datos["sub"]


def _huella(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


def _evento(conn, tipo: str, agregado: str, datos: dict):
    conn.execute("INSERT INTO pagos.outbox (tipo, agregado_id, datos) VALUES (%s, %s, %s)", (tipo, agregado, Jsonb(datos)))


class Cobro(BaseModel):
    pedido_id: str
    monto: Decimal = Field(gt=0)


@app.post("/v1/cobros", status_code=202, dependencies=[Depends(servicio)], summary="API-PAG-01 SolicitarCobro")
def solicitar_cobro(cuerpo: Cobro, idempotency_key: str = Header(...)):
    with tx() as conn:
        previo = conn.execute("SELECT pago_id, pedido_id, monto, estado FROM pagos.pago WHERE clave = %s", (idempotency_key,)).fetchone()
        if previo:   # reintento con la misma K2: se devuelve lo guardado, NO se vuelve a llamar a la pasarela
            if str(previo["pedido_id"]) != cuerpo.pedido_id or previo["monto"] != cuerpo.monto:
                raise HTTPException(422, "CLAVE_REUTILIZADA")
            return {**previo, "repetida": True}
        try:
            pago = conn.execute(
                """INSERT INTO pagos.pago (pedido_id, clave, monto, estado) VALUES (%s, %s, %s, 'SOLICITADO')
                   RETURNING pago_id""", (cuerpo.pedido_id, idempotency_key, cuerpo.monto)).fetchone()
        except pg_errores.UniqueViolation as e:  # pago_un_vivo_por_pedido: ya hay un cobro vivo para el pedido
            raise HTTPException(409, "PAGO_EN_CURSO") from e
    pago_id = str(pago["pago_id"])
    try:
        r = pasarela.cobrar(str(cuerpo.monto), ajustes.modo_cobro)          # ÚNICA llamada por K2
    except pasarela.TimeoutPasarela:
        with tx() as conn:
            conn.execute("UPDATE pagos.pago SET estado = 'INCIERTO' WHERE pago_id = %s AND estado = 'SOLICITADO'", (pago_id,))
            _evento(conn, "PagoIncierto", pago_id, {"pago_id": pago_id, "pedido_id": cuerpo.pedido_id, "estado": "INCIERTO"})
        return {"pago_id": pago_id, "pedido_id": cuerpo.pedido_id, "estado": "INCIERTO"}
    return registrar_resultado(pago_id, r["notificacion_id"], r["resultado"], r["aprobado_en"], r)


def registrar_resultado(pago_id: str, n1: str, resultado: str, aprobado_en: str | None, contenido: dict) -> dict:
    """API-PAG-02 RegistrarResultadoPago. N1 se guarda antes de procesar; un resultado contradictorio queda como conflicto."""
    with tx() as conn:
        pago = conn.execute("SELECT * FROM pagos.pago WHERE pago_id = %s FOR UPDATE", (pago_id,)).fetchone()
        if not pago:
            raise HTTPException(404, "PAGO_DESCONOCIDO")
        nueva = conn.execute(
            """INSERT INTO pagos.notificacion_pago (notificacion_id, pago_id, resultado, aprobado_en, contenido)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (notificacion_id) DO NOTHING RETURNING notificacion_id""",
            (n1, pago_id, resultado, aprobado_en, Jsonb(contenido))).fetchone()
        if nueva and pago["estado"] in ("SOLICITADO", "INCIERTO", "EN_REVISION_FINANZAS"):
            conn.execute("""UPDATE pagos.pago SET estado = %s, aprobado_en_pasarela = %s, recibido_en = clock_timestamp(),
                            referencia_pasarela = %s WHERE pago_id = %s""",
                         (resultado, aprobado_en, contenido.get("referencia"), pago_id))
            _evento(conn, f"Pago{resultado.capitalize()}", pago_id,
                    {"pago_id": pago_id, "pedido_id": str(pago["pedido_id"]), "estado": resultado,
                     "notificacion_id": n1, "aprobado_en_pasarela": aprobado_en})
        elif nueva and pago["estado"] != resultado:
            conn.execute("UPDATE pagos.notificacion_pago SET conflicto = true WHERE notificacion_id = %s", (n1,))
        estado = conn.execute("SELECT estado FROM pagos.pago WHERE pago_id = %s", (pago_id,)).fetchone()["estado"]
    return {"pago_id": pago_id, "pedido_id": str(pago["pedido_id"]), "estado": estado, "notificacion_id": n1}


class Notificacion(BaseModel):
    pago_id: str
    notificacion_id: str
    resultado: str = Field(pattern="^(APROBADO|RECHAZADO)$")
    aprobado_en: str | None = None


@app.post("/v1/notificaciones", dependencies=[Depends(servicio)], summary="Notificación de la pasarela (o del simulador)")
def notificacion(n: Notificacion):
    return registrar_resultado(n.pago_id, n.notificacion_id, n.resultado, n.aprobado_en, n.model_dump())


class Ajuste(BaseModel):
    pedido_id: str
    componente: str = Field(pattern="^(MERCANCIA|ENVIO|PROPINA|PROMOCION)$")
    tipo: str = Field(pattern="^(PARCIAL|TOTAL|ESCALADO)$")
    monto: Decimal = Field(gt=0)
    motivo: str


@app.post("/v1/ajustes", status_code=202, dependencies=[Depends(servicio)], summary="API-PAG-04 SolicitarAjusteCobro")
def solicitar_ajuste(a: Ajuste, idempotency_key: str = Header(...)):
    with tx() as conn:
        previo = conn.execute("SELECT * FROM pagos.ajuste_cobro WHERE clave = %s", (idempotency_key,)).fetchone()
        if previo:
            return {**previo, "repetida": True}
        pago = conn.execute("SELECT pago_id, monto FROM pagos.pago WHERE pedido_id = %s AND estado = 'APROBADO'", (a.pedido_id,)).fetchone()
        if not pago:
            raise HTTPException(409, "SIN_PAGO_APROBADO")
        ya = conn.execute("SELECT COALESCE(sum(monto), 0) AS s FROM pagos.ajuste_cobro WHERE pago_id = %s AND estado <> 'FALLIDO'",
                          (pago["pago_id"],)).fetchone()["s"]
        if ya + a.monto > pago["monto"]:
            raise HTTPException(422, "MONTO_EXCEDE_COBRADO")
        estado_inicial = "PENDIENTE_MANUAL" if a.componente != "MERCANCIA" else "SOLICITADO"   # PD05: lo no definido se escala
        ajuste = conn.execute(
            """INSERT INTO pagos.ajuste_cobro (pago_id, pedido_id, tipo, componente, monto, motivo, estado, clave)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING ajuste_id""",
            (pago["pago_id"], a.pedido_id, "ESCALADO" if a.componente != "MERCANCIA" else a.tipo, a.componente,
             a.monto, a.motivo, estado_inicial, idempotency_key)).fetchone()
    ajuste_id = str(ajuste["ajuste_id"])
    if estado_inicial == "PENDIENTE_MANUAL":
        return {"ajuste_id": ajuste_id, "estado": estado_inicial}
    try:
        r = pasarela.reembolsar(str(a.monto), a.tipo == "PARCIAL", ajustes.modo_reembolso)   # ÚNICA llamada por K4
        nuevo, ref = "APLICADO", r["referencia"]
    except pasarela.TimeoutPasarela:
        nuevo, ref = "INCIERTO", None
    except pasarela.ReembolsoParcialNoSoportado:
        nuevo, ref = "PENDIENTE_MANUAL", None
    with tx() as conn:
        conn.execute("""UPDATE pagos.ajuste_cobro SET estado = %s, referencia_externa = %s,
                        aplicado_en = CASE WHEN %s = 'APLICADO' THEN clock_timestamp() END WHERE ajuste_id = %s""",
                     (nuevo, ref, nuevo, ajuste_id))
        _evento(conn, f"AjusteCobro{nuevo.title().replace('_', '')}", ajuste_id,
                {"ajuste_id": ajuste_id, "pedido_id": a.pedido_id, "estado": nuevo, "monto": str(a.monto)})
    return {"ajuste_id": ajuste_id, "estado": nuevo}


class Resolucion(BaseModel):
    pago_id: str | None = None
    ajuste_id: str | None = None
    resultado: str = Field(pattern="^(APROBADO|RECHAZADO|APLICADO)$")
    referencia_externa: str
    evidencia: str = Field(min_length=5)


@app.post("/v1/resoluciones", summary="API-PAG-05 RegistrarResolucionManual (Finanzas)")
def resolver(r: Resolucion, usuario: str = Depends(finanzas)):
    if r.pago_id:
        with tx() as conn:
            conn.execute("""UPDATE pagos.pago SET resuelto_por = %s, evidencia_resolucion = %s WHERE pago_id = %s
                            AND estado IN ('INCIERTO','EN_REVISION_FINANZAS')""", (usuario, r.evidencia, r.pago_id))
        return registrar_resultado(r.pago_id, f"MANUAL-{r.referencia_externa}", r.resultado, None,
                                   {"referencia": r.referencia_externa, "resuelto_por": usuario})
    with tx() as conn:
        n = conn.execute("""UPDATE pagos.ajuste_cobro SET estado = 'APLICADO', referencia_externa = %s, usuario_manual = %s,
                            aplicado_en = clock_timestamp() WHERE ajuste_id = %s
                            AND estado IN ('INCIERTO','EN_REVISION_FINANZAS','PENDIENTE_MANUAL') RETURNING pedido_id""",
                         (r.referencia_externa, usuario, r.ajuste_id)).fetchone()
        if not n:
            raise HTTPException(409, "ESTADO_INVALIDO")
        _evento(conn, "AjusteCobroAplicado", r.ajuste_id, {"ajuste_id": r.ajuste_id, "pedido_id": str(n["pedido_id"]), "estado": "APLICADO"})
    return {"ajuste_id": r.ajuste_id, "estado": "APLICADO"}


@app.get("/v1/pagos", dependencies=[Depends(servicio)], summary="Pagos finales para el ciclo de reparación (ADR-12)")
def listar(estado: list[str] = Query(...), desde: str = Query(...)):
    with tx() as conn:
        filas = conn.execute(
            """SELECT p.pago_id::text, p.pedido_id::text, p.estado,
                      (SELECT n.notificacion_id FROM pagos.notificacion_pago n WHERE n.pago_id = p.pago_id AND NOT n.conflicto
                        ORDER BY n.recibido_en LIMIT 1) AS notificacion_id
                 FROM pagos.pago p WHERE p.estado = ANY(%s) AND p.solicitado_en >= %s ORDER BY p.solicitado_en""",
            (estado, desde)).fetchall()
    return {"pagos": filas}


@app.get("/salud")
def salud():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"estado": "ok"}
