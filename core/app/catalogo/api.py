"""M1 · Catálogo: productos, tiendas y disponibilidad para el código postal del cliente.
Es una consulta de lectura a la autoridad (M3); no reserva nada. La decisión de compra la toma siempre la reserva
transaccional del checkout (ADR-09 sin caché, AR-04)."""
from fastapi import APIRouter, Query

from ..db import transaccion
from ..errores import ErrorNegocio

router = APIRouter(prefix="/v1", tags=["Catálogo (M1)"])


@router.get("/tiendas", summary="API-CAT-01 ListarTiendas")
def tiendas():
    with transaccion() as conn:
        return {"tiendas": conn.execute(
            """SELECT d.tienda_id, d.nombre, d.activa,
                      COALESCE(array_agg(z.codigo_postal ORDER BY z.codigo_postal) FILTER (WHERE z.activa), '{}') AS codigos_postales
                 FROM inventario.dark_store d LEFT JOIN pedidos.zona_cobertura z USING (tienda_id)
                GROUP BY d.tienda_id ORDER BY d.tienda_id""").fetchall()}


@router.get("/catalogo", summary="API-CAT-02 CatalogoPorCodigoPostal (productos con disponibilidad en las tiendas que cubren el CP)")
def catalogo(codigo_postal: str = Query(pattern=r"^[0-9]{5}$")):
    with transaccion() as conn:
        tiendas = conn.execute(
            """SELECT z.tienda_id, d.nombre, z.distancia_km FROM pedidos.zona_cobertura z
                 JOIN inventario.dark_store d USING (tienda_id)
                WHERE z.codigo_postal = %s AND z.activa AND d.activa ORDER BY z.distancia_km, z.tienda_id""",
            (codigo_postal,)).fetchall()
        if not tiendas:
            raise ErrorNegocio("SIN_COBERTURA", f"Ninguna Dark Store cubre el código postal {codigo_postal}")
        filas = conn.execute(
            """SELECT p.sku, p.descripcion, p.precio, e.tienda_id, e.estado,
                      inventario.disponible(e.tienda_id, p.sku) AS disponible
                 FROM catalogo.producto p
                 JOIN inventario.existencia e ON e.sku = p.sku AND e.tienda_id = ANY(%s)
                WHERE p.activo ORDER BY p.sku""", ([t["tienda_id"] for t in tiendas],)).fetchall()
    orden = {t["tienda_id"]: i for i, t in enumerate(tiendas)}
    productos: dict[str, dict] = {}
    for f in filas:
        p = productos.setdefault(f["sku"], {"sku": f["sku"], "descripcion": f["descripcion"], "precio": f["precio"],
                                            "disponible": 0, "tienda_id": None, "por_tienda": []})
        p["por_tienda"].append({"tienda_id": f["tienda_id"], "disponible": f["disponible"], "estado": f["estado"]})
    for p in productos.values():
        p["por_tienda"].sort(key=lambda x: orden[x["tienda_id"]])
        # Lo que se muestra es lo de la tienda más cercana que lo tenga; el checkout asigna la tienda con ADR-13
        mejor = next((t for t in p["por_tienda"] if t["disponible"] > 0), None)
        if mejor:
            p["disponible"], p["tienda_id"] = mejor["disponible"], mejor["tienda_id"]
        p["bloqueado"] = all(t["estado"] != "NORMAL" for t in p["por_tienda"])
    return {"codigo_postal": codigo_postal, "tiendas": tiendas, "productos": list(productos.values()),
            "nota": "Vista de consulta: la compra la decide la reserva de 300 s en el checkout."}
