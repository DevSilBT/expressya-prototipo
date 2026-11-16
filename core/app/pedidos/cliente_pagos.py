"""Cliente HTTP hacia el servicio de Pagos (M4). Una sola llamada por K2; un timeout NO se reintenta (ADR-05, ADR-08)."""
import httpx

from ..config import ajustes


class ClientePagos:
    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=base_url or ajustes.pagos_url, timeout=ajustes.timeout_pagos_s,
                                  headers={"X-Servicio-Token": ajustes.token_servicio}, transport=transport)

    def solicitar_cobro(self, pedido_id: str, monto: str, clave_k2: str, corr: str) -> dict:
        """Devuelve el estado informado por Pagos o {'estado': 'INCIERTO'} si no hubo respuesta."""
        try:
            r = self._http.post("/v1/cobros", json={"pedido_id": pedido_id, "monto": monto},
                                headers={"Idempotency-Key": clave_k2, "X-Correlation-Id": corr})
        except httpx.TimeoutException:
            return {"estado": "INCIERTO", "motivo": "TIMEOUT_HACIA_PAGOS"}
        if r.status_code == 409:
            return {"estado": "PAGO_EN_CURSO"}
        r.raise_for_status()
        return r.json()

    def solicitar_ajuste(self, datos: dict, clave_k4: str, corr: str | None) -> dict:
        r = self._http.post("/v1/ajustes", json=datos, headers={"Idempotency-Key": clave_k4, "X-Correlation-Id": corr or ""})
        r.raise_for_status()
        return r.json()

    def pagos_finales(self, desde: str) -> list[dict]:
        r = self._http.get("/v1/pagos", params={"estado": ["APROBADO", "RECHAZADO"], "desde": desde})
        r.raise_for_status()
        return r.json()["pagos"]


cliente_pagos = ClientePagos()
