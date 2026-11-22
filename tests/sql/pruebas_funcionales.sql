\set ON_ERROR_STOP 1
-- Pruebas de la autoridad de inventario contra PostgreSQL real. Cada bloque lanza excepción si falla.

-- TP-S3-05 · Frontera de R-D02: 299.999 s se confirma, 300.000 s no (Cliente 1-nov, punto 12)
DO $$
DECLARE t0 timestamptz := '2026-11-02 10:00:00-06'; a jsonb; b jsonb; ca jsonb; cb jsonb; v_comp int;
BEGIN
  a := inventario.solicitar_reserva(gen_random_uuid(), 2::smallint, '[{"sku":"SKU-Y","cantidad":1}]', 'K1-a-d02', 'PRUEBA', NULL, t0);
  b := inventario.solicitar_reserva(gen_random_uuid(), 2::smallint, '[{"sku":"SKU-Y","cantidad":1}]', 'K1-b-d02', 'PRUEBA', NULL, t0);
  ca := inventario.confirmar_reserva((a->>'reserva_id')::uuid, 'N1-a', 'PRUEBA', NULL, t0 + interval '299.999 seconds');
  cb := inventario.confirmar_reserva((b->>'reserva_id')::uuid, 'N1-b', 'PRUEBA', NULL, t0 + interval '300 seconds');
  SELECT comprometido INTO v_comp FROM inventario.existencia WHERE tienda_id = 2 AND sku = 'SKU-Y';
  IF ca->>'resultado' <> 'CONFIRMADA' OR cb->>'resultado' <> 'RESERVA_NO_ACTIVA' OR cb->>'estado' <> 'EXPIRADA' OR v_comp <> 1 THEN
    RAISE EXCEPTION 'FALLA TP-S3-05: % / % / comprometido=%', ca, cb, v_comp;
  END IF;
  RAISE NOTICE 'TP-S3-05 OK: 299.999 s CONFIRMADA; 300.000 s EXPIRADA sin COMPROMISO; comprometido=%', v_comp;
END $$;

-- TP-S3-06 · Idempotencia: misma clave y misma solicitud → mismo resultado; misma clave con otra solicitud → CLAVE_REUTILIZADA
DO $$
DECLARE p uuid := gen_random_uuid(); r1 jsonb; r2 jsonb; r3 jsonb; c1 jsonb; c2 jsonb; n int;
BEGIN
  r1 := inventario.solicitar_reserva(p, 2::smallint, '[{"sku":"SKU-Z","cantidad":2}]', 'K1-idem', 'PRUEBA', NULL);
  r2 := inventario.solicitar_reserva(p, 2::smallint, '[{"sku":"SKU-Z","cantidad":2}]', 'K1-idem', 'PRUEBA', NULL);
  r3 := inventario.solicitar_reserva(p, 2::smallint, '[{"sku":"SKU-Z","cantidad":3}]', 'K1-idem', 'PRUEBA', NULL);
  c1 := inventario.confirmar_reserva((r1->>'reserva_id')::uuid, 'N1-idem', 'PRUEBA', NULL);
  c2 := inventario.confirmar_reserva((r1->>'reserva_id')::uuid, 'N1-idem', 'PRUEBA', NULL);
  SELECT count(*) INTO n FROM inventario.movimiento WHERE doc_id = r1->>'reserva_id' AND tipo = 'COMPROMISO' AND saldo = 'comprometido';
  IF r1->>'reserva_id' <> r2->>'reserva_id' OR (r2->>'repetida')::boolean IS NOT TRUE OR r3->>'error' <> 'CLAVE_REUTILIZADA'
     OR c2->>'resultado' <> 'CONFIRMADA' OR n <> 1 THEN
    RAISE EXCEPTION 'FALLA TP-S3-06: % % % % n=%', r1, r2, r3, c2, n;
  END IF;
  RAISE NOTICE 'TP-S3-06 OK: reintento devuelve la misma reserva; otra huella → CLAVE_REUTILIZADA; N1 repetida → un solo COMPROMISO';
END $$;

-- TP-S3-07 · Vencimiento por plazo y barrido: la unidad vuelve a estar disponible aunque el barrido no haya corrido
DO $$
DECLARE t0 timestamptz := clock_timestamp() - interval '10 minutes'; r jsonb; d_antes int; d_despues int; n int; est text;
BEGIN
  d_antes := inventario.disponible(2::smallint, 'SKU-X');
  r := inventario.solicitar_reserva(gen_random_uuid(), 2::smallint, '[{"sku":"SKU-X","cantidad":4}]', 'K1-exp', 'PRUEBA', NULL, t0);
  d_despues := inventario.disponible(2::smallint, 'SKU-X');           -- ya vencida por plazo: no cuenta
  n := inventario.expirar_vencidas();
  SELECT estado INTO est FROM inventario.reserva WHERE reserva_id = (r->>'reserva_id')::uuid;
  IF d_antes <> d_despues OR n < 1 OR est <> 'EXPIRADA' THEN RAISE EXCEPTION 'FALLA TP-S3-07 % % % %', d_antes, d_despues, n, est; END IF;
  RAISE NOTICE 'TP-S3-07 OK: disponible antes=% después=% (sin barrido); barrido expiró % reserva(s)', d_antes, d_despues, n;
END $$;

-- TP-S3-08 · Sobrecompromiso por merma (CA-16): vendible 3, 2 reservas de 1, merma 2 → déficit 1, SKU bloqueado
DO $$
DECLARE r jsonb; m jsonb; e inventario.existencia; d int;
BEGIN
  INSERT INTO catalogo.producto VALUES ('SKU-W', 'Producto W', 10) ON CONFLICT DO NOTHING;
  PERFORM inventario.cargar_existencia(3::smallint, 'SKU-W', 3);
  PERFORM inventario.solicitar_reserva(gen_random_uuid(), 3::smallint, '[{"sku":"SKU-W","cantidad":1}]', 'K1-w1', 'PRUEBA', NULL);
  PERFORM inventario.solicitar_reserva(gen_random_uuid(), 3::smallint, '[{"sku":"SKU-W","cantidad":1}]', 'K1-w2', 'PRUEBA', NULL);
  m := inventario.registrar_movimiento(3::smallint, 'SKU-W', 'MERMA', 2, 'DANO_MANIPULACION', 'usuario-encargado', gen_random_uuid(), 'K5-w', NULL);
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = 3 AND sku = 'SKU-W';
  d := inventario.disponible(3::smallint, 'SKU-W');
  r := inventario.solicitar_reserva(gen_random_uuid(), 3::smallint, '[{"sku":"SKU-W","cantidad":1}]', 'K1-w3', 'PRUEBA', NULL);
  IF e.vendible <> 1 OR e.estado <> 'SOBRECOMPROMETIDA' OR (m->>'deficit')::int <> 1 OR d <> 0 OR r->>'resultado' <> 'SKU_BLOQUEADO' THEN
    RAISE EXCEPTION 'FALLA TP-S3-08 % % % %', e, m, d, r;
  END IF;
  -- M2 libera la reserva más reciente sin pago en curso → recupera cobertura → NORMAL
  PERFORM inventario.liberar_reserva(r2.reserva_id, 'AGOTAMIENTO', 'K-lib-w', 'M2', NULL)
     FROM (SELECT reserva_id FROM inventario.reserva WHERE clave = 'K1-w2') r2;
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = 3 AND sku = 'SKU-W';
  IF e.estado <> 'NORMAL' THEN RAISE EXCEPTION 'FALLA TP-S3-08 (regreso a NORMAL) %', e; END IF;
  RAISE NOTICE 'TP-S3-08 OK: déficit 1 visible, SKU bloqueado; tras liberar por agotamiento vuelve a NORMAL';
END $$;

-- TP-S3-09 · Historial inmutable y aritmética de movimientos (INV-08, INV-18, DT-09)
DO $$
DECLARE ok boolean := false; malos int;
BEGIN
  BEGIN
    UPDATE inventario.movimiento SET delta = 99 WHERE movimiento_id = (SELECT min(movimiento_id) FROM inventario.movimiento);
  EXCEPTION WHEN raise_exception THEN ok := true; END;
  SELECT count(*) INTO malos FROM inventario.movimiento WHERE saldo_despues <> saldo_antes + delta;
  IF NOT ok OR malos <> 0 THEN RAISE EXCEPTION 'FALLA TP-S3-09'; END IF;
  -- secuencia contigua del saldo reservas_vigentes por (tienda, sku)
  SELECT count(*) INTO malos FROM (
    SELECT saldo_antes, lag(saldo_despues) OVER (PARTITION BY tienda_id, sku, saldo ORDER BY movimiento_id) AS previo
      FROM inventario.movimiento WHERE saldo IN ('reservas_vigentes','comprometido','vendible')) x
   WHERE previo IS NOT NULL AND previo <> saldo_antes;
  IF malos <> 0 THEN RAISE EXCEPTION 'FALLA TP-S3-09: % huecos en la secuencia de saldos', malos; END IF;
  RAISE NOTICE 'TP-S3-09 OK: UPDATE rechazado; aritmética y secuencia de saldos consistentes';
END $$;

-- TP-S3-10 · Restricciones físicas: reserva distinta de 300 s y línea que viola INV-16 se rechazan
DO $$
DECLARE r1 boolean := false; r2 boolean := false;
BEGIN
  BEGIN
    INSERT INTO inventario.solicitud_reserva (pedido_id, tienda_id, clave) VALUES (gen_random_uuid(), 1, 'x');
    INSERT INTO inventario.reserva (pedido_id, tienda_id, creada_en, expira_en, clave, solicitud_seq)
    VALUES (gen_random_uuid(), 1, now(), now() + interval '301 seconds', 'K1-301', currval(pg_get_serial_sequence('inventario.solicitud_reserva','solicitud_seq')));
  EXCEPTION WHEN check_violation THEN r1 := true; END;
  BEGIN
    INSERT INTO pedidos.pedido (cliente_id, tienda_id, codigo_postal, estado, subtotal_mercancia, clave_checkout)
    VALUES (gen_random_uuid(), 1, '53150', 'PAGADO', 25, 'K1-inv16');
    INSERT INTO pedidos.linea_pedido (pedido_id, sku, cantidad_pedida, precio_unitario, cantidad_surtida, cantidad_cancelada)
    VALUES ((SELECT pedido_id FROM pedidos.pedido WHERE clave_checkout = 'K1-inv16'), 'SKU-X', 2, 25, 2, 1);
  EXCEPTION WHEN check_violation THEN r2 := true; END;
  IF NOT (r1 AND r2) THEN RAISE EXCEPTION 'FALLA TP-S3-10 % %', r1, r2; END IF;
  RAISE NOTICE 'TP-S3-10 OK: CHECK de 300 s e INV-16 aplicados por la base de datos';
END $$;

