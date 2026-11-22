-- TP-S3-12 · Reconstrucción del saldo desde movimientos (C1): para TODAS las existencias, cada saldo recalculado
-- sumando su historial coincide con el registrado, después de todas las pruebas anteriores.
DO $$
DECLARE malos int; total int;
BEGIN
  SELECT count(*), count(*) FILTER (WHERE NOT r.coincide) INTO total, malos
    FROM inventario.existencia e CROSS JOIN LATERAL inventario.reconstruir_saldos(e.tienda_id, e.sku) r;
  IF malos <> 0 THEN RAISE EXCEPTION 'FALLA TP-S3-12: % de % saldos no se reconstruyen', malos, total; END IF;
  RAISE NOTICE 'TP-S3-12 OK: % saldos (vendible, comprometido, reservas) reconstruidos desde movimientos sin diferencias', total;
END $$;
SELECT e.tienda_id, e.sku, r.saldo, r.movimientos, r.desde_movimientos, r.registrado, r.coincide
  FROM inventario.existencia e CROSS JOIN LATERAL inventario.reconstruir_saldos(e.tienda_id, e.sku) r
 WHERE e.tienda_id IN (1, 2) ORDER BY 1, 2, 3;
