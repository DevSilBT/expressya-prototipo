-- C3 · pruebas SQL de surtido, faltante validado, conteo físico y ajuste autorizado (AT-07, AT-08, AT-09)
-- Actores: picker, supervisor, encargado y auditor son UUID distintos.
\set picker     '''11111111-1111-1111-1111-111111111111'''
\set supervisor '''22222222-2222-2222-2222-222222222222'''
\set encargado  '''33333333-3333-3333-3333-333333333333'''

-- TP-C3-01 · AT-07: lógica 10, física 8 → discrepancia registrada, SKU bloqueado; ajuste solo con otra persona
DO $$
DECLARE r jsonb; a jsonb; v_aj uuid; ok boolean := false; e inventario.existencia; d int; res jsonb; m inventario.movimiento;
BEGIN
  PERFORM inventario.cargar_existencia(4::smallint, 'SKU-X', 2);                       -- 8 + 2 = 10 lógicas
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = 4 AND sku = 'SKU-X';
  IF e.vendible <> 10 THEN RAISE EXCEPTION 'FALLA TP-C3-01: vendible inicial %', e.vendible; END IF;
  r := operacion.registrar_conteo(4::smallint, 'SKU-X', 8, '11111111-1111-1111-1111-111111111111', 'K-conteo-1', NULL);
  v_aj := (r->>'ajuste_id')::uuid;
  d := inventario.disponible(4::smallint, 'SKU-X');
  res := inventario.solicitar_reserva(gen_random_uuid(), 4::smallint, '[{"sku":"SKU-X","cantidad":1}]', 'K1-c3-bloq', 'PRUEBA', NULL);
  IF r->>'resultado' <> 'DISCREPANCIA' OR (r->>'diferencia')::int <> -2 OR r->>'estado_existencia' <> 'EN_VERIFICACION'
     OR d <> 0 OR res->>'resultado' <> 'SKU_BLOQUEADO' THEN
    RAISE EXCEPTION 'FALLA TP-C3-01 (detección): % disp=% reserva=%', r, d, res->>'resultado';
  END IF;
  BEGIN   -- quien contó no puede autorizar su propio ajuste (INV-14)
    PERFORM operacion.autorizar_ajuste(v_aj, '11111111-1111-1111-1111-111111111111', true, 'mismo usuario', 'K-aut-0', NULL);
  EXCEPTION WHEN raise_exception THEN ok := SQLERRM = 'AUTORIZADOR_IGUAL_A_SOLICITANTE'; END;
  a := operacion.autorizar_ajuste(v_aj, '33333333-3333-3333-3333-333333333333', true, 'Reconteo con foto del anaquel', 'K-aut-1', NULL);
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = 4 AND sku = 'SKU-X';
  SELECT * INTO m FROM inventario.movimiento WHERE movimiento_id = (a->>'movimiento_id')::bigint;
  IF NOT ok OR a->>'resultado' <> 'APLICADO' OR e.vendible <> 8 OR e.estado <> 'NORMAL' OR m.tipo <> 'AJUSTE_NEG' OR m.delta <> -2
     OR m.actor <> '33333333-3333-3333-3333-333333333333'
     OR EXISTS (SELECT 1 FROM inventario.discrepancia WHERE tienda_id = 4 AND sku = 'SKU-X' AND estado = 'ABIERTA')
     OR (operacion.autorizar_ajuste(v_aj, '33333333-3333-3333-3333-333333333333', true, 'Reconteo con foto del anaquel', 'K-aut-1', NULL)->>'repetida')::boolean IS NOT TRUE THEN
    RAISE EXCEPTION 'FALLA TP-C3-01 (ajuste): ok=% % vendible=% estado=%', ok, a, e.vendible, e.estado;
  END IF;
  RAISE NOTICE 'TP-C3-01 OK: lógica 10 / física 8 → discrepancia -2, SKU bloqueado (disp 0, SKU_BLOQUEADO); mismo usuario rechazado; ajuste autorizado por otra persona → vendible 8, NORMAL';
END $$;

-- Pedido pagado de cinco líneas en la tienda 3 (X, Y, Z con 5 unidades; A y B con 10): A×1, B×1, Y×1, Z×1 y X×2
CREATE TEMP TABLE c3 AS SELECT gen_random_uuid() AS pedido_id;
DO $$
DECLARE p uuid := (SELECT pedido_id FROM c3); r jsonb; v_lineas jsonb;
BEGIN
  INSERT INTO pedidos.pedido (pedido_id, cliente_id, tienda_id, codigo_postal, estado, subtotal_mercancia, envio, propina, clave_checkout)
  VALUES (p, gen_random_uuid(), 3, '53200', 'RESERVADO', 30 + 12 + 40 + 15.5 + 2 * 25, 35, 10, 'K1-c3-pedido');
  INSERT INTO pedidos.linea_pedido (pedido_id, sku, cantidad_pedida, precio_unitario, estado)
  SELECT p, sku, q, precio, 'RESERVADA' FROM (VALUES ('SKU-A', 1), ('SKU-B', 1), ('SKU-Y', 1), ('SKU-Z', 1), ('SKU-X', 2)) v(sku, q)
  JOIN catalogo.producto USING (sku);
  SELECT jsonb_agg(jsonb_build_object('linea_id', linea_id, 'sku', sku, 'cantidad', cantidad_pedida)) INTO v_lineas
    FROM pedidos.linea_pedido WHERE pedido_id = p;
  r := inventario.solicitar_reserva(p, 3::smallint, v_lineas, 'K1-c3-res', 'PRUEBA', NULL);
  r := inventario.confirmar_reserva((r->>'reserva_id')::uuid, 'N1-c3', 'PRUEBA', NULL);
  IF r->>'resultado' <> 'CONFIRMADA' THEN RAISE EXCEPTION 'preparación de datos: %', r; END IF;
  UPDATE pedidos.pedido SET estado = 'PAGADO' WHERE pedido_id = p;
  UPDATE pedidos.linea_pedido SET estado = 'COMPROMETIDA' WHERE pedido_id = p;
END $$;

-- TP-C3-02 · AT-08 y AT-09: cinco líneas, faltante en una; las otras cuatro continúan; cancelación parcial y un solo reembolso
DO $$
DECLARE p uuid := (SELECT pedido_id FROM c3); l record; r jsonb; v jsonb; f uuid; ok boolean := false; ok2 boolean := false;
        v_estado text; n_aj int; v_monto text; e inventario.existencia; lx uuid;
BEGIN
  r := operacion.iniciar_preparacion(p, '11111111-1111-1111-1111-111111111111', NULL);
  FOR l IN SELECT linea_id FROM pedidos.linea_pedido WHERE pedido_id = p AND sku <> 'SKU-X' LOOP
    r := operacion.surtir_linea(l.linea_id, 1, '11111111-1111-1111-1111-111111111111', 'K-surt-' || l.linea_id, NULL);
  END LOOP;
  SELECT linea_id INTO lx FROM pedidos.linea_pedido WHERE pedido_id = p AND sku = 'SKU-X';
  r := operacion.reportar_faltante(lx, 1, 1, 'NO_ENCONTRADO', NULL, '11111111-1111-1111-1111-111111111111', 'K3-c3', NULL);
  f := (r->>'faltante_id')::uuid;
  IF r->>'resultado' <> 'POR_VALIDAR' OR inventario.disponible(3::smallint, 'SKU-X') <> 0
     OR (SELECT estado FROM pedidos.pedido WHERE pedido_id = p) <> 'EN_PREPARACION' THEN
    RAISE EXCEPTION 'FALLA TP-C3-02 (reporte): %', r;
  END IF;
  r := operacion.surtir_linea(lx, 1, '11111111-1111-1111-1111-111111111111', 'K-surt-x', NULL);          -- la unidad encontrada
  BEGIN
    PERFORM operacion.validar_faltante(f, '11111111-1111-1111-1111-111111111111', true, 'yo mismo', 'K-val-0', NULL);
  EXCEPTION WHEN raise_exception THEN ok := SQLERRM = 'VALIDADOR_IGUAL_A_PICKER'; END;
  v := operacion.validar_faltante(f, '22222222-2222-2222-2222-222222222222', true, 'Revisé anaquel y bodega', 'K-val-1', NULL);
  -- reintento con la misma clave y nuevo intento con otra clave: ningún efecto adicional
  r := operacion.validar_faltante(f, '22222222-2222-2222-2222-222222222222', true, 'Revisé anaquel y bodega', 'K-val-1', NULL);
  BEGIN
    PERFORM operacion.validar_faltante(f, '22222222-2222-2222-2222-222222222222', true, 'otra vez', 'K-val-2', NULL);
  EXCEPTION WHEN raise_exception THEN ok2 := SQLERRM = 'FALTANTE_YA_RESUELTO'; END;
  SELECT estado INTO v_estado FROM pedidos.pedido WHERE pedido_id = p;
  SELECT count(*), max(datos->>'monto') INTO n_aj, v_monto FROM comun.outbox
   WHERE agregado_id = p::text AND tipo = 'SolicitudAjusteCobro' AND datos->>'componente' = 'MERCANCIA';
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = 3 AND sku = 'SKU-X';
  IF NOT ok OR NOT ok2 OR v->>'resultado' <> 'VALIDADO' OR (r->>'repetida')::boolean IS NOT TRUE OR v_estado <> 'PARCIAL'
     OR n_aj <> 1 OR v_monto <> '25.00' OR e.estado <> 'NORMAL' OR e.comprometido <> 0
     OR (SELECT count(*) FROM pedidos.linea_pedido WHERE pedido_id = p AND estado = 'SURTIDA') <> 4
     OR (SELECT cantidad_surtida || '/' || cantidad_cancelada || ' ' || estado FROM pedidos.linea_pedido WHERE linea_id = lx) <> '1/1 SURTIDA_PARCIAL'
     OR (SELECT count(*) FROM pedidos.cancelacion_linea WHERE linea_id = lx AND causa = 'FALTANTE_PICKING') <> 1 THEN
    RAISE EXCEPTION 'FALLA TP-C3-02: ok=% ok2=% v=% pedido=% ajustes=% monto=% existencia=%', ok, ok2, v, v_estado, n_aj, v_monto, e;
  END IF;
  RAISE NOTICE 'TP-C3-02 OK: 5 líneas, 1 unidad faltante → 4 líneas SURTIDA + 1 SURTIDA_PARCIAL, pedido PARCIAL; validación por el mismo picker rechazada; 1 cancelación y 1 reembolso de 25.00 (solo mercancía) aunque se valide dos veces';
END $$;

-- TP-C3-03 · Faltante NO validado: la venta sigue bloqueada hasta que un conteo dirigido resuelve la discrepancia
DO $$
DECLARE p uuid := gen_random_uuid(); l uuid; r jsonb; f uuid; c jsonb;
BEGIN
  INSERT INTO pedidos.pedido (pedido_id, cliente_id, tienda_id, codigo_postal, estado, subtotal_mercancia, clave_checkout)
  VALUES (p, gen_random_uuid(), 5, '53400', 'RESERVADO', 24, 'K1-c3-p2');
  INSERT INTO pedidos.linea_pedido (pedido_id, sku, cantidad_pedida, precio_unitario) VALUES (p, 'SKU-B', 2, 12) RETURNING linea_id INTO l;
  r := inventario.solicitar_reserva(p, 5::smallint, jsonb_build_array(jsonb_build_object('linea_id', l, 'sku', 'SKU-B', 'cantidad', 2)), 'K1-c3-r2', 'PRUEBA', NULL);
  PERFORM inventario.confirmar_reserva((r->>'reserva_id')::uuid, 'N1-c3-2', 'PRUEBA', NULL);
  UPDATE pedidos.pedido SET estado = 'PAGADO' WHERE pedido_id = p;
  PERFORM operacion.iniciar_preparacion(p, '11111111-1111-1111-1111-111111111111', NULL);
  f := (operacion.reportar_faltante(l, 0, 2, 'UBICACION_INCORRECTA', NULL, '11111111-1111-1111-1111-111111111111', 'K3-c3-2', NULL)->>'faltante_id')::uuid;
  r := operacion.validar_faltante(f, '22222222-2222-2222-2222-222222222222', false, 'Estaba en otro pasillo', 'K-val-3', NULL);
  IF (SELECT estado FROM inventario.existencia WHERE tienda_id = 5 AND sku = 'SKU-B') <> 'EN_VERIFICACION'
     OR (SELECT estado FROM pedidos.linea_pedido WHERE linea_id = l) <> 'COMPROMETIDA' THEN
    RAISE EXCEPTION 'FALLA TP-C3-03 (no validado): %', r;
  END IF;
  c := operacion.registrar_conteo(5::smallint, 'SKU-B', 10, '33333333-3333-3333-3333-333333333333', 'K-conteo-2', NULL);
  r := operacion.surtir_linea(l, 2, '11111111-1111-1111-1111-111111111111', 'K-surt-b2', NULL);
  IF c->>'resultado' <> 'SIN_DIFERENCIA' OR c->>'estado_existencia' <> 'NORMAL' OR r->>'estado_pedido' <> 'COMPLETO' THEN
    RAISE EXCEPTION 'FALLA TP-C3-03 (conteo): % %', c, r;
  END IF;
  RAISE NOTICE 'TP-C3-03 OK: faltante no validado → SKU sigue bloqueado; conteo dirigido sin diferencia lo libera; pedido COMPLETO';
END $$;

-- TP-C3-04 · Historial: quién, qué, cuándo, por qué y a qué pedido afectó (tienda 3, SKU-X)
SELECT m.movimiento_id, m.tipo, m.saldo, m.delta, m.saldo_antes || '→' || m.saldo_despues AS saldo, left(m.actor, 8) AS actor,
       m.motivo, COALESCE(CASE WHEN m.doc_tipo = 'PEDIDO' THEN m.doc_id::uuid END, r.pedido_id) IS NOT NULL AS con_pedido
  FROM inventario.movimiento m LEFT JOIN inventario.reserva r ON m.doc_tipo = 'RESERVA' AND r.reserva_id::text = m.doc_id
 WHERE m.tienda_id = 3 AND m.sku = 'SKU-X' ORDER BY m.movimiento_id;
