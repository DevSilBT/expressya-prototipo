-- Dos órdenes de líneas opuestos sobre los mismos SKU: el bloqueo en orden de SKU debe evitar bloqueos mutuos
\set orden random(0, 1)
SELECT inventario.solicitar_reserva(gen_random_uuid(), 4::smallint,
  CASE WHEN :orden = 0 THEN '[{"sku":"SKU-Y","cantidad":1},{"sku":"SKU-Z","cantidad":1}]'::jsonb
                       ELSE '[{"sku":"SKU-Z","cantidad":1},{"sku":"SKU-Y","cantidad":1}]'::jsonb END,
  'K1-' || gen_random_uuid(), 'PRUEBA_ORDEN', gen_random_uuid());
