-- Bitácora del cliente en tiempo real (ADR-29 propuesto): API-PED-10 lee la bandeja de salida por pedido.
-- Solo agrega un índice (el esquema solo crece; no cambia datos ni reglas).
CREATE INDEX IF NOT EXISTS outbox_por_pedido ON comun.outbox ((datos->>'pedido_id'), creado_en);
CREATE INDEX IF NOT EXISTS notificacion_por_pedido ON notificaciones.notificacion ((datos->>'pedido_id'), creada_en);
