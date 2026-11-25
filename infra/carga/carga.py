#!/usr/bin/env python3
"""ExpressYa · medición de rendimiento y pruebas de volumen (solo biblioteca estándar de Python 3.8+).

Corre igual desde Azure Cloud Shell, desde una laptop en la red de la escuela o en el CI, para COMPARAR redes:
  python3 infra/carga/carga.py --url https://<core> latencia             # dónde se va el tiempo: servidor vs. red
  python3 infra/carga/carga.py --url https://<core> lectura  -n 300 -c 20 # volumen de consultas al catálogo
  python3 infra/carga/carga.py --url https://<core> checkout -n 60  -c 15 # reservas concurrentes (no paga)
  python3 infra/carga/carga.py --url https://<core> todo                   # los tres, con valores por omisión

- Mide el tiempo total que ve el cliente y el tiempo DENTRO del servidor (encabezado Server-Timing). La diferencia es
  red + TLS + arranque en frío: así se distingue si la lentitud es de la red (p. ej. PC Puma), de Azure o de la base.
- «checkout» usa la sesión simulada (no existe en producción), aparta 1 unidad por cliente y NO paga: las reservas
  vencen solas a los 5 min y el inventario regresa. Comprueba que nunca se aparten más unidades de las disponibles.
- Límites de seguridad (costo ≤ 1 dólar y base B1ms de 35 conexiones): máx. 2 000 solicitudes y 30 concurrentes.
- Guarda la evidencia en carga_<escenario>_<fecha>.json.
"""
import argparse
import concurrent.futures as cf
import json
import re
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

MAX_SOLICITUDES, MAX_CONCURRENCIA = 2000, 30
CTX = ssl.create_default_context()


def http(url, metodo="GET", cuerpo=None, cab=None, timeout=90):
    req = urllib.request.Request(url, method=metodo, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                 headers={"Content-Type": "application/json", **(cab or {})})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            datos, estado, h = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        datos, estado, h = e.read(), e.code, e.headers
    except Exception as e:                                        # timeout, conexión rechazada, DNS…
        return {"estado": 0, "ms": (time.perf_counter() - t0) * 1000, "servidor_ms": None, "error": type(e).__name__, "json": {}}
    ms = (time.perf_counter() - t0) * 1000
    m = re.search(r"dur=([\d.]+)", h.get("Server-Timing", "") or "")
    try:
        js = json.loads(datos or b"{}")
    except ValueError:
        js = {}
    return {"estado": estado, "ms": ms, "servidor_ms": float(m.group(1)) if m else None, "json": js}


def pct(v, p):
    if not v:
        return None
    v = sorted(v)
    k = (len(v) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def resumen(nombre, res, segundos):
    ok = [r for r in res if 200 <= r["estado"] < 300]
    ms = [r["ms"] for r in res]
    srv = [r["servidor_ms"] for r in res if r["servidor_ms"] is not None]
    red = [r["ms"] - r["servidor_ms"] for r in res if r["servidor_ms"] is not None]
    estados = {}
    for r in res:
        k = str(r["estado"]) if r["estado"] else r.get("error", "ERROR")
        estados[k] = estados.get(k, 0) + 1
    f = lambda x: "—" if x is None else f"{x:8.1f}"
    s = {"escenario": nombre, "solicitudes": len(res), "exitosas": len(ok), "por_estado": estados,
         "duracion_s": round(segundos, 2), "solicitudes_por_s": round(len(res) / segundos, 2) if segundos else None,
         "total_ms": {"p50": pct(ms, 50), "p90": pct(ms, 90), "p95": pct(ms, 95), "p99": pct(ms, 99), "max": max(ms) if ms else None},
         "servidor_ms": {"p50": pct(srv, 50), "p95": pct(srv, 95), "max": max(srv) if srv else None},
         "red_y_espera_ms": {"p50": pct(red, 50), "p95": pct(red, 95)}}
    print(f"\n== {nombre}: {len(res)} solicitudes en {segundos:.1f} s ({s['solicitudes_por_s']} sol/s) · estados {estados}")
    print(f"   total que ve el cliente (ms)  p50 {f(s['total_ms']['p50'])}  p90 {f(s['total_ms']['p90'])}  "
          f"p95 {f(s['total_ms']['p95'])}  p99 {f(s['total_ms']['p99'])}  máx {f(s['total_ms']['max'])}")
    print(f"   dentro del servidor (ms)      p50 {f(s['servidor_ms']['p50'])}  p95 {f(s['servidor_ms']['p95'])}  máx {f(s['servidor_ms']['max'])}")
    print(f"   red + TLS + espera (ms)       p50 {f(s['red_y_espera_ms']['p50'])}  p95 {f(s['red_y_espera_ms']['p95'])}")
    return s


def latencia(url, repeticiones=8):
    """Secuencial: la primera solicitud puede incluir arranque en frío; el resto mide la operación en caliente."""
    rutas = ["/salud", "/v1/tiendas", "/v1/catalogo?codigo_postal=53400", "/app"]
    filas, salida = [], []
    for ruta in rutas:
        res = [http(url + ruta) for _ in range(repeticiones)]
        primera, resto = res[0], res[1:]
        srv = pct([r["servidor_ms"] for r in resto if r["servidor_ms"] is not None], 50)
        fila = {"ruta": ruta, "primera_ms": round(primera["ms"], 1), "primera_servidor_ms": primera["servidor_ms"],
                "caliente_p50_ms": round(pct([r["ms"] for r in resto], 50) or 0, 1),
                "caliente_servidor_p50_ms": None if srv is None else round(srv, 1),
                "estados": sorted({r["estado"] for r in res})}
        fila["red_p50_ms"] = None if srv is None else round(fila["caliente_p50_ms"] - srv, 1)
        filas.append(fila); salida += res
    print("\n== latencia (secuencial)")
    print(f"   {'ruta':<34}{'1.ª (ms)':>10}{'caliente p50':>14}{'servidor p50':>14}{'red p50':>10}  estados")
    for x in filas:
        g = lambda v, w: f"{'—':>{w}}" if v is None else f"{v:>{w}.1f}"
        print(f"   {x['ruta']:<34}{x['primera_ms']:>10.1f}{x['caliente_p50_ms']:>14.1f}{g(x['caliente_servidor_p50_ms'], 14)}"
              f"{g(x['red_p50_ms'], 10)}  {x['estados']}")
    print("   Lectura: si «red» domina, la demora es de la conexión (red local, distancia a centralus, TLS); si domina")
    print("   «servidor», es de la aplicación o de la base; si solo la 1.ª es lenta, es el arranque en frío (escala a cero).")
    return {"escenario": "latencia", "rutas": filas}


def en_paralelo(fn, n, c):
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=c) as ex:
        res = list(ex.map(lambda i: fn(i), range(n)))
    return res, time.perf_counter() - t0


def lectura(url, n, c):
    res, seg = en_paralelo(lambda i: http(url + "/v1/catalogo?codigo_postal=53400"), n, c)
    return resumen("lectura de catálogo (CP 53400)", res, seg)


def checkout(url, n, c, sku="SKU-A", cp="53400"):
    antes = http(f"{url}/v1/catalogo?codigo_postal={cp}")["json"]
    disp = next((p["disponible"] for p in antes.get("productos", []) if p["sku"] == sku), None)
    if disp is None:
        print(f"   No se pudo leer la disponibilidad de {sku} (¿URL o CP?)."); return {"escenario": "checkout", "error": "sin_catalogo"}
    tokens = []
    for i in range(n):
        s = http(url + "/v1/demo/sesion", "POST", {"nombre": f"carga-{i}", "rol": "CLIENTE"})
        if s["estado"] != 200:
            print("   La sesión simulada no está disponible (¿producción?): no se ejecuta checkout."); return {"escenario": "checkout", "error": "sin_sesion"}
        tokens.append(s["json"]["token"])
    cuerpo = {"codigo_postal": cp, "lineas": [{"sku": sku, "cantidad": 1}], "envio": 35, "propina": 0}
    res, seg = en_paralelo(lambda i: http(url + "/v1/pedidos", "POST", cuerpo,
                                          {"Authorization": tokens[i], "Idempotency-Key": f"carga-{uuid.uuid4()}"}), n, c)
    s = resumen(f"checkout concurrente: {n} clientes, 1 × {sku} cada uno (disponibles antes: {disp})", res, seg)
    aceptadas = sum(1 for r in res if r["estado"] == 201)
    agotado = sum(1 for r in res if r["estado"] == 409)
    s.update({"disponible_antes": disp, "reservas_aceptadas": aceptadas, "agotado": agotado,
              "sin_sobreventa": aceptadas <= disp, "errores_5xx": sum(1 for r in res if r["estado"] >= 500 or r["estado"] == 0)})
    print(f"   reservas aceptadas {aceptadas} de {disp} disponibles · AGOTADO {agotado} · "
          f"{'SIN SOBREVENTA ✓' if s['sin_sobreventa'] else 'SOBREVENTA ✗'} · errores 5xx/red {s['errores_5xx']}")
    print("   Las reservas vencen solas a los 5 min (no se pagó): el inventario regresa sin intervención.")
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("escenario", choices=["latencia", "lectura", "checkout", "todo"])
    ap.add_argument("--url", required=True)
    ap.add_argument("-n", "--solicitudes", type=int, default=None)
    ap.add_argument("-c", "--concurrencia", type=int, default=10)
    ap.add_argument("--sku", default="SKU-A")
    a = ap.parse_args()
    url = a.url.rstrip("/")
    c = min(a.concurrencia, MAX_CONCURRENCIA)
    n = lambda d: min(a.solicitudes or d, MAX_SOLICITUDES)
    print(f"ExpressYa · {a.escenario} contra {url} · {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC · concurrencia {c}")
    resultados = []
    if a.escenario in ("latencia", "todo"):
        resultados.append(latencia(url))
    if a.escenario in ("lectura", "todo"):
        resultados.append(lectura(url, n(200), c))
    if a.escenario in ("checkout", "todo"):
        resultados.append(checkout(url, n(40), c, a.sku))
    nombre = f"carga_{a.escenario}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    with open(nombre, "w", encoding="utf-8") as fh:
        json.dump({"url": url, "concurrencia": c, "resultados": resultados}, fh, ensure_ascii=False, indent=1, default=str)
    print(f"\nEvidencia guardada en {nombre}")
    malos = [r for r in resultados if r.get("sin_sobreventa") is False or r.get("errores_5xx")]
    sys.exit(1 if malos else 0)


if __name__ == "__main__":
    main()
