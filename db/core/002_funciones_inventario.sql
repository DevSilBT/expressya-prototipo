-- ExpressYa · Funciones transaccionales de la autoridad de inventario (M3) · ADR-19
-- Todas se ejecutan en READ COMMITTED con bloqueo pesimista de filas (FOR UPDATE) en orden de SKU (ADR-18).
-- El reloj que decide plazos es el del servidor de base de datos: clock_timestamp() (ADR-20, R-D02).
-- p_ahora solo existe para pruebas automatizadas; la API nunca lo envía.

-- ---------------------------------------------------------------- idempotencia (ADR-05)
CREATE FUNCTION comun.reclamar_clave(p_clave text, p_tipo text, p_huella text) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_row comun.operacion_procesada;
BEGIN
  INSERT INTO comun.operacion_procesada (clave, tipo, huella) VALUES (p_clave, p_tipo, p_huella)
  ON CONFLICT (clave) DO NOTHING;
  IF FOUND THEN RETURN NULL; END IF;                 -- clave nueva: el llamador ejecuta el efecto
  -- La clave ya existía: si otra transacción la estaba usando, el INSERT esperó a que terminara.
  SELECT * INTO v_row FROM comun.operacion_procesada WHERE clave = p_clave;
  IF v_row.huella <> p_huella OR v_row.tipo <> p_tipo THEN
    RETURN jsonb_build_object('resultado', 'ERROR', 'error', 'CLAVE_REUTILIZADA');
  END IF;
  IF v_row.resultado IS NULL THEN                    -- solo posible dentro de la misma transacción
    RETURN jsonb_build_object('resultado', 'ERROR', 'error', 'OPERACION_EN_CURSO');
  END IF;
  RETURN v_row.resultado || jsonb_build_object('repetida', true);
END $$;

CREATE FUNCTION comun.guardar_resultado(p_clave text, p_resultado jsonb) RETURNS jsonb
LANGUAGE sql AS $$
  UPDATE comun.operacion_procesada SET resultado = p_resultado WHERE clave = p_clave RETURNING resultado;
$$;

-- ---------------------------------------------------------------- lecturas
CREATE FUNCTION inventario.reservas_vigentes(p_tienda smallint, p_sku text, p_ahora timestamptz) RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(sum(cantidad), 0)::int FROM inventario.reserva_linea
  WHERE tienda_id = p_tienda AND sku = p_sku AND vigente AND expira_en > p_ahora;   -- vencimiento por plazo (ADR-04)
$$;

-- INV-01: disponibilidad = máx(0, vendible − reservas vigentes − comprometido); 0 si no está NORMAL
CREATE FUNCTION inventario.disponible(p_tienda smallint, p_sku text, p_ahora timestamptz DEFAULT clock_timestamp())
RETURNS integer LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN e.estado <> 'NORMAL' THEN 0
              ELSE GREATEST(0, e.vendible - e.comprometido - inventario.reservas_vigentes(e.tienda_id, e.sku, p_ahora)) END
  FROM inventario.existencia e WHERE e.tienda_id = p_tienda AND e.sku = p_sku;
$$;

-- ---------------------------------------------------------------- utilidades internas
CREATE FUNCTION inventario._mov(p_tienda smallint, p_sku text, p_tipo text, p_saldo text, p_delta int, p_antes int,
                                p_actor text, p_doc_tipo text, p_doc_id text, p_corr uuid, p_motivo text DEFAULT NULL)
RETURNS bigint LANGUAGE sql AS $$
  INSERT INTO inventario.movimiento (tienda_id, sku, tipo, saldo, delta, saldo_antes, saldo_despues, motivo, actor, doc_tipo, doc_id, correlacion_id)
  VALUES (p_tienda, p_sku, p_tipo, p_saldo, p_delta, p_antes, p_antes + p_delta, p_motivo, p_actor, p_doc_tipo, p_doc_id, p_corr)
  RETURNING movimiento_id;
$$;

CREATE FUNCTION comun.publicar(p_modulo text, p_tipo text, p_agregado text, p_corr uuid, p_datos jsonb) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO comun.outbox (modulo, tipo, agregado_id, correlacion_id, datos) VALUES (p_modulo, p_tipo, p_agregado, p_corr, p_datos);
$$;

-- Reevalúa INV-03 de una existencia ya bloqueada y la devuelve a NORMAL si recuperó cobertura
CREATE FUNCTION inventario._reevaluar(p_tienda smallint, p_sku text, p_ahora timestamptz, p_corr uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE e inventario.existencia;
BEGIN
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = p_tienda AND sku = p_sku;
  IF e.estado = 'SOBRECOMPROMETIDA'
     AND e.comprometido + inventario.reservas_vigentes(p_tienda, p_sku, p_ahora) <= e.vendible THEN
    UPDATE inventario.existencia SET estado = 'NORMAL', actualizado_en = p_ahora WHERE tienda_id = p_tienda AND sku = p_sku;
    UPDATE inventario.sobrecompromiso SET estado = 'RESUELTO', resuelto_en = p_ahora
     WHERE tienda_id = p_tienda AND sku = p_sku AND estado = 'ABIERTO';
    PERFORM comun.publicar('M3', 'ExistenciaCambioEstado', p_tienda || ':' || p_sku, p_corr,
            jsonb_build_object('tienda_id', p_tienda, 'sku', p_sku, 'estado', 'NORMAL'));
  END IF;
END $$;

-- Cierra una reserva ACTIVA ya bloqueada (LIBERADA, EXPIRADA o LIBERADA_AGOTAMIENTO)
CREATE FUNCTION inventario._cerrar_reserva(p_reserva uuid, p_estado text, p_tipo_mov text, p_motivo text,
                                           p_actor text, p_corr uuid, p_ahora timestamptz) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE l record; v_antes int; v_pedido uuid;
BEGIN
  -- Orden de bloqueo global: reserva → existencias en orden de SKU (evita bloqueos mutuos)
  PERFORM 1 FROM inventario.existencia e
   WHERE (e.tienda_id, e.sku) IN (SELECT tienda_id, sku FROM inventario.reserva_linea WHERE reserva_id = p_reserva)
   ORDER BY e.sku FOR UPDATE;
  FOR l IN SELECT * FROM inventario.reserva_linea WHERE reserva_id = p_reserva ORDER BY sku LOOP
    v_antes := inventario.reservas_vigentes(l.tienda_id, l.sku, '-infinity');   -- reservas aún marcadas vigentes
    UPDATE inventario.reserva_linea SET vigente = false WHERE reserva_id = p_reserva AND linea_id = l.linea_id;
    PERFORM inventario._mov(l.tienda_id, l.sku, p_tipo_mov, 'reservas_vigentes', -l.cantidad, v_antes,
                            p_actor, 'RESERVA', p_reserva::text, p_corr, p_motivo);
    PERFORM inventario._reevaluar(l.tienda_id, l.sku, p_ahora, p_corr);
  END LOOP;
  UPDATE inventario.reserva SET estado = p_estado, cerrada_en = p_ahora, motivo_cierre = p_motivo
   WHERE reserva_id = p_reserva RETURNING pedido_id INTO v_pedido;
  PERFORM comun.publicar('M3', CASE p_estado WHEN 'EXPIRADA' THEN 'ReservaExpirada'
                                             WHEN 'LIBERADA_AGOTAMIENTO' THEN 'ReservaLiberadaPorAgotamiento'
                                             ELSE 'ReservaLiberada' END,
                         p_reserva::text, p_corr, jsonb_build_object('reserva_id', p_reserva, 'pedido_id', v_pedido, 'motivo', p_motivo));
END $$;

-- ---------------------------------------------------------------- API-INV-02 SolicitarReserva
CREATE FUNCTION inventario.solicitar_reserva(p_pedido uuid, p_tienda smallint, p_lineas jsonb, p_clave text,
                                             p_actor text, p_corr uuid, p_ahora timestamptz DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_prev jsonb; v_seq bigint; v_ahora timestamptz; v_reserva uuid; v_res jsonb;
  v_fallas jsonb := '[]'::jsonb; v_bloq jsonb := '[]'::jsonb; l record; e inventario.existencia; v_disp int; v_antes int;
  v_huella text := encode(digest(p_pedido::text || '|' || p_tienda || '|' || p_lineas::text, 'sha256'), 'hex');
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'SolicitarReserva', v_huella);
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;

  IF jsonb_typeof(p_lineas) <> 'array' OR jsonb_array_length(p_lineas) = 0
     OR EXISTS (SELECT 1 FROM jsonb_to_recordset(p_lineas) AS x(sku text, cantidad int) WHERE x.cantidad IS NULL OR x.cantidad <= 0)
     OR (SELECT count(*) <> count(DISTINCT x.sku) FROM jsonb_to_recordset(p_lineas) AS x(sku text)) THEN
    RETURN comun.guardar_resultado(p_clave, jsonb_build_object('resultado', 'ERROR', 'error', 'CANTIDAD_INVALIDA'));
  END IF;

  -- RN09: toda solicitud queda registrada con marca del servidor y número secuencial
  INSERT INTO inventario.solicitud_reserva (pedido_id, tienda_id, clave) VALUES (p_pedido, p_tienda, p_clave)
  RETURNING solicitud_seq INTO v_seq;

  -- Bloqueo pesimista de las existencias del pedido en orden de SKU (ADR-18)
  PERFORM 1 FROM inventario.existencia
   WHERE tienda_id = p_tienda AND sku IN (SELECT x.sku FROM jsonb_to_recordset(p_lineas) AS x(sku text))
   ORDER BY sku FOR UPDATE;
  v_ahora := COALESCE(p_ahora, clock_timestamp());   -- la hora cuenta después de obtener el bloqueo

  FOR l IN SELECT * FROM jsonb_to_recordset(p_lineas) AS x(linea_id uuid, sku text, cantidad int) ORDER BY x.sku LOOP
    SELECT * INTO e FROM inventario.existencia WHERE tienda_id = p_tienda AND sku = l.sku;
    IF NOT FOUND THEN
      v_fallas := v_fallas || jsonb_build_object('sku', l.sku, 'solicitada', l.cantidad, 'disponible', 0);
    ELSIF e.estado <> 'NORMAL' THEN
      v_bloq := v_bloq || jsonb_build_object('sku', l.sku, 'estado', e.estado);
    ELSE
      v_disp := inventario.disponible(p_tienda, l.sku, v_ahora);
      IF v_disp < l.cantidad THEN
        v_fallas := v_fallas || jsonb_build_object('sku', l.sku, 'solicitada', l.cantidad, 'disponible', v_disp);
      END IF;
    END IF;
  END LOOP;

  IF jsonb_array_length(v_bloq) > 0 OR jsonb_array_length(v_fallas) > 0 THEN
    UPDATE inventario.solicitud_reserva SET resultado = CASE WHEN jsonb_array_length(v_bloq) > 0 THEN 'SKU_BLOQUEADO' ELSE 'AGOTADO' END
     WHERE solicitud_seq = v_seq;
    v_res := jsonb_build_object('resultado', CASE WHEN jsonb_array_length(v_bloq) > 0 THEN 'SKU_BLOQUEADO' ELSE 'AGOTADO' END,
                                'lineas', v_fallas || v_bloq, 'solicitud_seq', v_seq);
    PERFORM comun.publicar('M3', 'SolicitudReservaRechazada', p_pedido::text, p_corr, v_res || jsonb_build_object('pedido_id', p_pedido));
    RETURN comun.guardar_resultado(p_clave, v_res);   -- no se reserva NADA (RF04)
  END IF;

  INSERT INTO inventario.reserva (pedido_id, tienda_id, creada_en, expira_en, clave, solicitud_seq)
  VALUES (p_pedido, p_tienda, v_ahora, v_ahora + interval '300 seconds', p_clave, v_seq)
  RETURNING reserva_id INTO v_reserva;

  FOR l IN SELECT * FROM jsonb_to_recordset(p_lineas) AS x(linea_id uuid, sku text, cantidad int) ORDER BY x.sku LOOP
    v_antes := inventario.reservas_vigentes(p_tienda, l.sku, '-infinity');   -- saldo contable: líneas marcadas vigentes
    INSERT INTO inventario.reserva_linea (reserva_id, linea_id, tienda_id, sku, cantidad, expira_en)
    VALUES (v_reserva, COALESCE(l.linea_id, gen_random_uuid()), p_tienda, l.sku, l.cantidad, v_ahora + interval '300 seconds');
    PERFORM inventario._mov(p_tienda, l.sku, 'RESERVA', 'reservas_vigentes', l.cantidad, v_antes,
                            p_actor, 'RESERVA', v_reserva::text, p_corr);
  END LOOP;
  UPDATE inventario.solicitud_reserva SET resultado = 'ACEPTADA' WHERE solicitud_seq = v_seq;

  v_res := jsonb_build_object('resultado', 'ACEPTADA', 'reserva_id', v_reserva, 'creada_en', v_ahora,
                              'expira_en', v_ahora + interval '300 seconds', 'solicitud_seq', v_seq);
  PERFORM comun.publicar('M3', 'ReservaCreada', v_reserva::text, p_corr, v_res || jsonb_build_object('pedido_id', p_pedido));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- API-INV-04 ConfirmarReserva (R-D02)
CREATE FUNCTION inventario.confirmar_reserva(p_reserva uuid, p_clave text, p_actor text, p_corr uuid,
                                             p_ahora timestamptz DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; r inventario.reserva; v_ahora timestamptz; l record; v_antes int; v_res jsonb;
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'ConfirmarReserva', encode(digest(p_reserva::text, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;

  SELECT * INTO r FROM inventario.reserva WHERE reserva_id = p_reserva FOR UPDATE;
  IF NOT FOUND THEN
    RETURN comun.guardar_resultado(p_clave, jsonb_build_object('resultado', 'ERROR', 'error', 'RESERVA_DESCONOCIDA'));
  END IF;
  v_ahora := COALESCE(p_ahora, clock_timestamp());

  IF r.estado = 'ACTIVA' AND v_ahora < r.expira_en THEN            -- 5:00.000 ya no es válido
    PERFORM 1 FROM inventario.existencia e
     WHERE (e.tienda_id, e.sku) IN (SELECT tienda_id, sku FROM inventario.reserva_linea WHERE reserva_id = p_reserva)
     ORDER BY e.sku FOR UPDATE;
    FOR l IN SELECT * FROM inventario.reserva_linea WHERE reserva_id = p_reserva ORDER BY sku LOOP
      SELECT comprometido INTO v_antes FROM inventario.existencia WHERE tienda_id = l.tienda_id AND sku = l.sku;
      UPDATE inventario.existencia SET comprometido = comprometido + l.cantidad, actualizado_en = v_ahora
       WHERE tienda_id = l.tienda_id AND sku = l.sku;
      PERFORM inventario._mov(l.tienda_id, l.sku, 'COMPROMISO', 'reservas_vigentes', -l.cantidad,
                              inventario.reservas_vigentes(l.tienda_id, l.sku, '-infinity'),
                              p_actor, 'RESERVA', p_reserva::text, p_corr, 'PAGO_APROBADO');
      UPDATE inventario.reserva_linea SET vigente = false WHERE reserva_id = p_reserva AND linea_id = l.linea_id;
      PERFORM inventario._mov(l.tienda_id, l.sku, 'COMPROMISO', 'comprometido', l.cantidad, v_antes,
                              p_actor, 'RESERVA', p_reserva::text, p_corr, 'PAGO_APROBADO');
    END LOOP;
    UPDATE inventario.reserva SET estado = 'CONFIRMADA', cerrada_en = v_ahora, motivo_cierre = 'PAGO_APROBADO'
     WHERE reserva_id = p_reserva;
    v_res := jsonb_build_object('resultado', 'CONFIRMADA', 'reserva_id', p_reserva, 'confirmada_en', v_ahora);
    PERFORM comun.publicar('M3', 'ReservaConfirmada', p_reserva::text, p_corr, v_res || jsonb_build_object('pedido_id', r.pedido_id));
  ELSIF r.estado = 'ACTIVA' THEN                                      -- vencida sin marcar: se expira aquí
    PERFORM inventario._cerrar_reserva(p_reserva, 'EXPIRADA', 'EXPIRACION', 'PLAZO_VENCIDO_AL_CONFIRMAR', p_actor, p_corr, v_ahora);
    v_res := jsonb_build_object('resultado', 'RESERVA_NO_ACTIVA', 'estado', 'EXPIRADA', 'hora_servidor', v_ahora, 'expira_en', r.expira_en);
  ELSE
    v_res := jsonb_build_object('resultado', 'RESERVA_NO_ACTIVA', 'estado', r.estado, 'hora_servidor', v_ahora);
  END IF;
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- API-INV-05 LiberarReserva
CREATE FUNCTION inventario.liberar_reserva(p_reserva uuid, p_motivo text, p_clave text, p_actor text, p_corr uuid,
                                           p_ahora timestamptz DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; r inventario.reserva; v_ahora timestamptz; v_res jsonb;
BEGIN
  IF p_motivo NOT IN ('PAGO_RECHAZADO','CLIENTE_CANCELA','AGOTAMIENTO') THEN
    RAISE EXCEPTION 'MOTIVO_INVALIDO' USING ERRCODE = 'P0001';
  END IF;
  v_prev := comun.reclamar_clave(p_clave, 'LiberarReserva', encode(digest(p_reserva::text || p_motivo, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  SELECT * INTO r FROM inventario.reserva WHERE reserva_id = p_reserva FOR UPDATE;
  IF NOT FOUND THEN
    RETURN comun.guardar_resultado(p_clave, jsonb_build_object('resultado', 'ERROR', 'error', 'RESERVA_DESCONOCIDA'));
  END IF;
  v_ahora := COALESCE(p_ahora, clock_timestamp());
  IF r.estado = 'ACTIVA' THEN
    PERFORM inventario._cerrar_reserva(p_reserva,
              CASE WHEN p_motivo = 'AGOTAMIENTO' THEN 'LIBERADA_AGOTAMIENTO' ELSE 'LIBERADA' END,
              CASE WHEN p_motivo = 'AGOTAMIENTO' THEN 'LIBERACION_AGOTAMIENTO' ELSE 'LIBERACION' END,
              p_motivo, p_actor, p_corr, v_ahora);
    v_res := jsonb_build_object('resultado', 'LIBERADA', 'reserva_id', p_reserva);
  ELSE
    v_res := jsonb_build_object('resultado', 'YA_CERRADA', 'estado', r.estado);
  END IF;
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- API-INV-06 ExpirarReservasVencidas (barrido)
CREATE FUNCTION inventario.expirar_vencidas(p_ahora timestamptz DEFAULT NULL, p_limite int DEFAULT 500)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE v_ahora timestamptz := COALESCE(p_ahora, clock_timestamp()); r record; n int := 0;
BEGIN
  FOR r IN SELECT reserva_id FROM inventario.reserva
            WHERE estado = 'ACTIVA' AND expira_en <= v_ahora
            ORDER BY expira_en LIMIT p_limite FOR UPDATE SKIP LOCKED LOOP   -- no compite con una confirmación en curso
    PERFORM inventario._cerrar_reserva(r.reserva_id, 'EXPIRADA', 'EXPIRACION', 'PLAZO_300S', 'BARRIDO', NULL, v_ahora);
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- ---------------------------------------------------------------- API-INV-09 RegistrarMovimiento (ya autorizado por M5)
CREATE FUNCTION inventario.registrar_movimiento(p_tienda smallint, p_sku text, p_tipo text, p_cantidad int, p_motivo text,
                                                p_actor text, p_ajuste uuid, p_clave text, p_corr uuid, p_ahora timestamptz DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; e inventario.existencia; v_ahora timestamptz; v_delta int; v_mov bigint; v_def int; v_res jsonb;
BEGIN
  IF p_tipo NOT IN ('MERMA','DANO','AJUSTE_NEG','AJUSTE_POS') OR p_cantidad <= 0 THEN
    RAISE EXCEPTION 'CANTIDAD_INVALIDA' USING ERRCODE = 'P0001';
  END IF;
  IF p_motivo IS NULL OR p_ajuste IS NULL THEN RAISE EXCEPTION 'MOTIVO_REQUERIDO' USING ERRCODE = 'P0001'; END IF;
  v_prev := comun.reclamar_clave(p_clave, 'RegistrarMovimiento',
            encode(digest(p_tienda || p_sku || p_tipo || p_cantidad || p_ajuste::text, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = p_tienda AND sku = p_sku FOR UPDATE;
  v_ahora := COALESCE(p_ahora, clock_timestamp());
  v_delta := CASE WHEN p_tipo = 'AJUSTE_POS' THEN p_cantidad ELSE -LEAST(p_cantidad, e.vendible) END;  -- INV-02: nunca < 0
  UPDATE inventario.existencia SET vendible = vendible + v_delta, actualizado_en = v_ahora WHERE tienda_id = p_tienda AND sku = p_sku;
  v_mov := inventario._mov(p_tienda, p_sku, p_tipo, 'vendible', v_delta, e.vendible, p_actor, 'AJUSTE_INVENTARIO', p_ajuste::text, p_corr, p_motivo);
  v_def := e.comprometido + inventario.reservas_vigentes(p_tienda, p_sku, v_ahora) - (e.vendible + v_delta);
  IF v_def > 0 THEN                                                   -- INV-03 roto: sobrecompromiso visible, no oculto
    UPDATE inventario.existencia SET estado = CASE WHEN estado = 'NORMAL' THEN 'SOBRECOMPROMETIDA' ELSE estado END
     WHERE tienda_id = p_tienda AND sku = p_sku;
    INSERT INTO inventario.sobrecompromiso (tienda_id, sku, deficit, movimiento_causa_id) VALUES (p_tienda, p_sku, v_def, v_mov)
    ON CONFLICT (tienda_id, sku) WHERE estado = 'ABIERTO' DO UPDATE SET deficit = EXCLUDED.deficit;
    -- M2 elige qué reserva liberar (conoce los pagos en curso) y llama a liberar_reserva con motivo AGOTAMIENTO (ADR-14)
    PERFORM comun.publicar('M3', 'SobrecompromisoDetectado', p_tienda || ':' || p_sku, p_corr,
            jsonb_build_object('tienda_id', p_tienda, 'sku', p_sku, 'deficit', v_def,
                               'reservas_activas', (SELECT jsonb_agg(jsonb_build_object('reserva_id', r.reserva_id, 'pedido_id', r.pedido_id, 'creada_en', r.creada_en) ORDER BY r.creada_en DESC)
                                                      FROM inventario.reserva r JOIN inventario.reserva_linea rl USING (reserva_id)
                                                     WHERE rl.tienda_id = p_tienda AND rl.sku = p_sku AND rl.vigente AND rl.expira_en > v_ahora)));
  ELSE
    PERFORM inventario._reevaluar(p_tienda, p_sku, v_ahora, p_corr);
  END IF;
  v_res := jsonb_build_object('resultado', 'APLICADO', 'movimiento_id', v_mov, 'deficit', GREATEST(v_def, 0));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;
