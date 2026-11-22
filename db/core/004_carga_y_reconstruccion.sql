-- C1 · Carga auditable de existencias y reconstrucción del saldo desde movimientos (INV-08, RF20)
-- 1) La carga de inventario también es un movimiento (antes la carga semilla escribía el saldo directamente).
ALTER TABLE inventario.movimiento DROP CONSTRAINT movimiento_tipo_check;
ALTER TABLE inventario.movimiento ADD CONSTRAINT movimiento_tipo_check CHECK (tipo IN ('RESERVA','LIBERACION','EXPIRACION',
  'LIBERACION_AGOTAMIENTO','COMPROMISO','LIBERACION_COMPROMISO','SURTIDO','FALTANTE','MERMA','DANO','AJUSTE_POS','AJUSTE_NEG',
  'ENTRADA_RECEPCION','VALIDACION_RECEPCION','DEVOLUCION_RECIBIDA','REINGRESO','MERMA_REVISION','CONTEO','CARGA_INICIAL'));

-- Carga (o incremento) de existencia vendible con su movimiento, en una sola transacción.
CREATE FUNCTION inventario.cargar_existencia(p_tienda smallint, p_sku text, p_cantidad integer,
                                             p_actor text DEFAULT 'SISTEMA', p_motivo text DEFAULT 'Carga de datos semilla')
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE v_antes integer;
BEGIN
  IF p_cantidad <= 0 THEN RAISE EXCEPTION 'cantidad debe ser positiva'; END IF;
  INSERT INTO inventario.existencia (tienda_id, sku, vendible) VALUES (p_tienda, p_sku, 0) ON CONFLICT DO NOTHING;
  SELECT vendible INTO v_antes FROM inventario.existencia WHERE tienda_id = p_tienda AND sku = p_sku FOR UPDATE;
  UPDATE inventario.existencia SET vendible = v_antes + p_cantidad WHERE tienda_id = p_tienda AND sku = p_sku;
  INSERT INTO inventario.movimiento (tienda_id, sku, tipo, saldo, delta, saldo_antes, saldo_despues, motivo, actor)
  VALUES (p_tienda, p_sku, 'CARGA_INICIAL', 'vendible', p_cantidad, v_antes, v_antes + p_cantidad, p_motivo, p_actor);
  RETURN v_antes + p_cantidad;
END $$;

-- 2) Regulariza la carga semilla anterior: existencias con vendible > 0 y sin ningún movimiento de vendible
--    reciben su movimiento CARGA_INICIAL (0 → vendible). No cambia ningún saldo.
INSERT INTO inventario.movimiento (tienda_id, sku, tipo, saldo, delta, saldo_antes, saldo_despues, motivo, actor)
SELECT e.tienda_id, e.sku, 'CARGA_INICIAL', 'vendible', e.vendible, 0, e.vendible, 'Regularización de la carga semilla', 'SISTEMA'
  FROM inventario.existencia e
 WHERE e.vendible > 0
   AND NOT EXISTS (SELECT 1 FROM inventario.movimiento m WHERE m.tienda_id = e.tienda_id AND m.sku = e.sku AND m.saldo = 'vendible')
 ORDER BY e.tienda_id, e.sku;

-- 3) Inventario para las cinco tiendas (C1 · «al menos cinco tiendas y productos»).
SELECT inventario.cargar_existencia(t::smallint, s, q)
  FROM (VALUES (3,'SKU-X',5),(3,'SKU-Y',5),(3,'SKU-Z',5),
               (4,'SKU-X',8),(4,'SKU-Y',8),(4,'SKU-Z',8),
               (5,'SKU-X',2),(5,'SKU-Y',12),(5,'SKU-Z',6)) AS v(t, s, q)
 WHERE NOT EXISTS (SELECT 1 FROM inventario.existencia e WHERE e.tienda_id = v.t AND e.sku = v.s);
INSERT INTO pedidos.zona_cobertura (tienda_id, codigo_postal, distancia_km) VALUES
  (3,'53200',1.0), (4,'53300',1.5), (5,'53400',2.0)
ON CONFLICT DO NOTHING;

-- 4) Reconstrucción: cada saldo se recalcula sumando los deltas de su historial y se compara con el registrado.
--    reservas_vigentes se compara con las líneas de reserva marcadas como vigentes (la marca la cierran
--    liberación, expiración y compromiso, todos con movimiento).
CREATE FUNCTION inventario.reconstruir_saldos(p_tienda smallint, p_sku text)
RETURNS TABLE (saldo text, desde_movimientos integer, registrado integer, movimientos integer, coincide boolean)
LANGUAGE sql STABLE AS $$
  WITH m AS (
    SELECT mv.saldo, COALESCE(sum(mv.delta), 0)::int AS total, count(*)::int AS n
      FROM inventario.movimiento mv WHERE mv.tienda_id = p_tienda AND mv.sku = p_sku GROUP BY mv.saldo),
  r AS (
    SELECT 'vendible'::text AS saldo, e.vendible AS registrado FROM inventario.existencia e WHERE e.tienda_id = p_tienda AND e.sku = p_sku
    UNION ALL SELECT 'comprometido', e.comprometido FROM inventario.existencia e WHERE e.tienda_id = p_tienda AND e.sku = p_sku
    UNION ALL SELECT 'reservas_vigentes', COALESCE((SELECT sum(cantidad)::int FROM inventario.reserva_linea
                                                     WHERE tienda_id = p_tienda AND sku = p_sku AND vigente), 0))
  SELECT r.saldo, COALESCE(m.total, 0), r.registrado, COALESCE(m.n, 0), COALESCE(m.total, 0) = r.registrado
    FROM r LEFT JOIN m USING (saldo) ORDER BY r.saldo;
$$;
