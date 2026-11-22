-- C3 · Surtido, discrepancia físico-lógica y cancelación parcial (M5 Operación)
-- Reglas: ADR-02 (el stock se descuenta al surtir), INV-14 y INV-19 (quien valida o autoriza es distinta persona de quien
-- reporta o solicita), INV-16 (surtida + cancelada ≤ pedida), INV-18 (una discrepancia se resuelve con un movimiento),
-- PD05 (solo la mercancía se reembolsa automáticamente), conteos cíclicos dirigidos (decisión del equipo, Semana 1).
-- Todas las operaciones con efecto son idempotentes por clave (comun.reclamar_clave) y publican su evento en la bandeja.

-- ---------------------------------------------------------------- utilidades
-- Recalcula el estado de una existencia ya bloqueada:
--   discrepancia abierta → EN_VERIFICACION (disponibilidad incierta bloqueada: inventario.disponible devuelve 0)
--   déficit (comprometido + reservas > vendible) → SOBRECOMPROMETIDA, visible en inventario.sobrecompromiso
--   en otro caso → NORMAL
CREATE FUNCTION operacion._estado_existencia(p_tienda smallint, p_sku text, p_corr uuid) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE e inventario.existencia; v_ahora timestamptz := clock_timestamp(); v_def int; v_mov bigint; v_estado text;
BEGIN
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = p_tienda AND sku = p_sku;
  IF EXISTS (SELECT 1 FROM inventario.discrepancia WHERE tienda_id = p_tienda AND sku = p_sku AND estado = 'ABIERTA') THEN
    v_estado := 'EN_VERIFICACION';
  ELSE
    v_def := e.comprometido + inventario.reservas_vigentes(p_tienda, p_sku, v_ahora) - e.vendible;
    IF v_def > 0 THEN
      v_estado := 'SOBRECOMPROMETIDA';
      SELECT max(movimiento_id) INTO v_mov FROM inventario.movimiento WHERE tienda_id = p_tienda AND sku = p_sku;
      INSERT INTO inventario.sobrecompromiso (tienda_id, sku, deficit, movimiento_causa_id) VALUES (p_tienda, p_sku, v_def, v_mov)
      ON CONFLICT (tienda_id, sku) WHERE estado = 'ABIERTO' DO UPDATE SET deficit = EXCLUDED.deficit;
      PERFORM comun.publicar('M3', 'SobrecompromisoDetectado', p_tienda || ':' || p_sku, p_corr,
              jsonb_build_object('tienda_id', p_tienda, 'sku', p_sku, 'deficit', v_def));
    ELSE
      v_estado := 'NORMAL';
      UPDATE inventario.sobrecompromiso SET estado = 'RESUELTO', resuelto_en = v_ahora
       WHERE tienda_id = p_tienda AND sku = p_sku AND estado = 'ABIERTO';
    END IF;
  END IF;
  IF v_estado <> e.estado THEN
    UPDATE inventario.existencia SET estado = v_estado, actualizado_en = v_ahora WHERE tienda_id = p_tienda AND sku = p_sku;
    PERFORM comun.publicar('M3', 'ExistenciaCambioEstado', p_tienda || ':' || p_sku, p_corr,
            jsonb_build_object('tienda_id', p_tienda, 'sku', p_sku, 'estado', v_estado, 'estado_anterior', e.estado));
  END IF;
  RETURN v_estado;
END $$;

CREATE FUNCTION operacion._notificar(p_destinatario text, p_tipo text, p_datos jsonb, p_clave text, p_canales text[])
RETURNS void LANGUAGE sql AS $$
  INSERT INTO notificaciones.notificacion (destinatario, tipo, canal, datos, clave)
  SELECT p_destinatario, p_tipo, c, p_datos, p_clave FROM unnest(p_canales) AS c
  ON CONFLICT (clave, canal) DO NOTHING;
$$;

-- Recalcula el estado de las líneas y, cuando ya no queda nada pendiente, cierra el pedido (COMPLETO, PARCIAL o
-- CANCELADO_TOTAL). Se llama con el pedido ya bloqueado.
CREATE FUNCTION operacion._evaluar_pedido(p_pedido uuid, p_corr uuid) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v_pend int; v_surt int; v_canc int; p pedidos.pedido; v_estado text;
BEGIN
  UPDATE pedidos.linea_pedido l SET estado = CASE
      WHEN l.cantidad_surtida = l.cantidad_pedida THEN 'SURTIDA'
      WHEN l.cantidad_surtida + l.cantidad_cancelada = l.cantidad_pedida AND l.cantidad_surtida = 0 THEN 'CANCELADA'
      WHEN l.cantidad_surtida + l.cantidad_cancelada = l.cantidad_pedida THEN 'SURTIDA_PARCIAL'
      WHEN EXISTS (SELECT 1 FROM operacion.reporte_faltante f WHERE f.linea_id = l.linea_id AND f.estado = 'POR_VALIDAR')
           THEN 'FALTANTE_REPORTADO'
      WHEN l.cantidad_surtida > 0 THEN 'SURTIDA_PARCIAL'
      ELSE 'COMPROMETIDA' END
   WHERE l.pedido_id = p_pedido;
  SELECT sum(cantidad_pedida - cantidad_surtida - cantidad_cancelada), sum(cantidad_surtida), sum(cantidad_cancelada)
    INTO v_pend, v_surt, v_canc FROM pedidos.linea_pedido WHERE pedido_id = p_pedido;
  SELECT * INTO p FROM pedidos.pedido WHERE pedido_id = p_pedido;
  IF v_pend > 0 THEN RETURN p.estado; END IF;
  v_estado := CASE WHEN v_surt = 0 THEN 'CANCELADO_TOTAL' WHEN v_canc > 0 THEN 'PARCIAL' ELSE 'COMPLETO' END;
  UPDATE pedidos.pedido SET estado = v_estado, actualizado_en = clock_timestamp() WHERE pedido_id = p_pedido;
  PERFORM comun.publicar('M2', 'PedidoPreparado', p_pedido::text, p_corr,
          jsonb_build_object('pedido_id', p_pedido, 'estado', v_estado, 'unidades_surtidas', v_surt, 'unidades_canceladas', v_canc));
  IF v_estado = 'CANCELADO_TOTAL' THEN
    -- PD05: envío y propina no se reembolsan automáticamente; Pagos los deja PENDIENTE_MANUAL para Finanzas
    IF p.envio > 0 THEN
      PERFORM comun.publicar('M2', 'SolicitudAjusteCobro', p_pedido::text, p_corr,
              jsonb_build_object('pedido_id', p_pedido, 'componente', 'ENVIO', 'tipo', 'TOTAL', 'monto', p.envio::text,
                                 'motivo', 'CANCELACION_TOTAL_POR_FALTANTE', 'clave', p_pedido || '#ENVIO'));
    END IF;
    IF p.propina > 0 THEN
      PERFORM comun.publicar('M2', 'SolicitudAjusteCobro', p_pedido::text, p_corr,
              jsonb_build_object('pedido_id', p_pedido, 'componente', 'PROPINA', 'tipo', 'TOTAL', 'monto', p.propina::text,
                                 'motivo', 'CANCELACION_TOTAL_POR_FALTANTE', 'clave', p_pedido || '#PROPINA'));
    END IF;
    PERFORM operacion._notificar(p.cliente_id::text, 'CANCELACION_TOTAL', jsonb_build_object('pedido_id', p_pedido, 'causa', 'FALTANTE'),
                                 'cancelacion_total:' || p_pedido, ARRAY['APP','CORREO']);
  END IF;
  RETURN v_estado;
END $$;

-- ---------------------------------------------------------------- API-OPS-01 IniciarPreparacion
CREATE FUNCTION operacion.iniciar_preparacion(p_pedido uuid, p_actor uuid, p_corr uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE p pedidos.pedido;
BEGIN
  SELECT * INTO p FROM pedidos.pedido WHERE pedido_id = p_pedido FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ENCONTRADO' USING ERRCODE = 'P0001'; END IF;
  IF p.estado = 'EN_PREPARACION' THEN RETURN jsonb_build_object('resultado', 'EN_PREPARACION', 'repetida', true); END IF;
  IF p.estado <> 'PAGADO' THEN RAISE EXCEPTION 'ESTADO_INVALIDO' USING ERRCODE = 'P0001', DETAIL = p.estado; END IF;
  UPDATE pedidos.pedido SET estado = 'EN_PREPARACION', actualizado_en = clock_timestamp() WHERE pedido_id = p_pedido;
  PERFORM comun.publicar('M5', 'PreparacionIniciada', p_pedido::text, p_corr,
          jsonb_build_object('pedido_id', p_pedido, 'tienda_id', p.tienda_id, 'picker_id', p_actor));
  RETURN jsonb_build_object('resultado', 'EN_PREPARACION', 'pedido_id', p_pedido);
END $$;

-- Bloquea línea, pedido (EN_PREPARACION) y existencia; devuelve lo pendiente de la línea
CREATE FUNCTION operacion._preparar_linea(p_linea uuid, OUT l pedidos.linea_pedido, OUT p pedidos.pedido,
                                          OUT e inventario.existencia, OUT pendiente int)
LANGUAGE plpgsql AS $$
BEGIN
  SELECT * INTO l FROM pedidos.linea_pedido WHERE linea_id = p_linea;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ENCONTRADO' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO p FROM pedidos.pedido WHERE pedido_id = l.pedido_id FOR UPDATE;          -- pedido → línea → existencia
  SELECT * INTO l FROM pedidos.linea_pedido WHERE linea_id = p_linea FOR UPDATE;
  IF p.estado <> 'EN_PREPARACION' THEN RAISE EXCEPTION 'ESTADO_INVALIDO' USING ERRCODE = 'P0001', DETAIL = p.estado; END IF;
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = p.tienda_id AND sku = l.sku FOR UPDATE;
  pendiente := l.cantidad_pedida - l.cantidad_surtida - l.cantidad_cancelada
             - (SELECT COALESCE(sum(faltantes), 0) FROM operacion.reporte_faltante WHERE linea_id = p_linea AND estado = 'POR_VALIDAR');
END $$;

-- ---------------------------------------------------------------- API-OPS-02 SurtirLinea (ADR-02: aquí se descuenta)
CREATE FUNCTION operacion.surtir_linea(p_linea uuid, p_cantidad int, p_picker uuid, p_clave text, p_corr uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; x record; v_ahora timestamptz := clock_timestamp(); v_res jsonb;
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'SurtirLinea', encode(digest(p_linea::text || ':' || p_cantidad, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  SELECT * INTO x FROM operacion._preparar_linea(p_linea);
  IF p_cantidad <= 0 OR p_cantidad > x.pendiente OR (x.e).comprometido < p_cantidad OR (x.e).vendible < p_cantidad THEN
    RAISE EXCEPTION 'CANTIDAD_INVALIDA' USING ERRCODE = 'P0001', DETAIL = 'pendiente ' || x.pendiente;
  END IF;
  UPDATE inventario.existencia SET vendible = vendible - p_cantidad, comprometido = comprometido - p_cantidad, actualizado_en = v_ahora
   WHERE tienda_id = (x.p).tienda_id AND sku = (x.l).sku;
  PERFORM inventario._mov((x.p).tienda_id, (x.l).sku, 'SURTIDO', 'vendible', -p_cantidad, (x.e).vendible,
                          p_picker::text, 'PEDIDO', (x.p).pedido_id::text, p_corr, 'SURTIDO');
  PERFORM inventario._mov((x.p).tienda_id, (x.l).sku, 'SURTIDO', 'comprometido', -p_cantidad, (x.e).comprometido,
                          p_picker::text, 'PEDIDO', (x.p).pedido_id::text, p_corr, 'SURTIDO');
  UPDATE pedidos.linea_pedido SET cantidad_surtida = cantidad_surtida + p_cantidad WHERE linea_id = p_linea;
  PERFORM comun.publicar('M5', 'LineaSurtida', (x.p).pedido_id::text, p_corr,
          jsonb_build_object('pedido_id', (x.p).pedido_id, 'tienda_id', (x.p).tienda_id, 'sku', (x.l).sku, 'cantidad', p_cantidad, 'picker_id', p_picker));
  v_res := jsonb_build_object('resultado', 'SURTIDA', 'linea_id', p_linea, 'cantidad', p_cantidad,
                              'estado_pedido', operacion._evaluar_pedido((x.p).pedido_id, p_corr));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- API-OPS-03 ReportarFaltante (picker, K3)
CREATE FUNCTION operacion.reportar_faltante(p_linea uuid, p_encontradas int, p_faltantes int, p_motivo text, p_descripcion text,
                                            p_picker uuid, p_clave text, p_corr uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; x record; v_disc uuid; v_fal uuid; v_res jsonb;
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'ReportarFaltante',
            encode(digest(p_linea::text || ':' || p_faltantes || ':' || p_motivo, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  SELECT * INTO x FROM operacion._preparar_linea(p_linea);
  IF p_faltantes <= 0 OR p_faltantes > x.pendiente OR p_encontradas < 0 THEN
    RAISE EXCEPTION 'CANTIDAD_INVALIDA' USING ERRCODE = 'P0001', DETAIL = 'pendiente ' || x.pendiente;
  END IF;
  -- Lógico: lo que el sistema dice que hay en anaquel; físico observado: faltan p_faltantes (se confirma con conteo dirigido)
  INSERT INTO inventario.discrepancia (tienda_id, sku, origen, cantidad_logica, cantidad_fisica)
  VALUES ((x.p).tienda_id, (x.l).sku, 'FALTANTE', (x.e).vendible, GREATEST((x.e).vendible - p_faltantes, 0))
  RETURNING discrepancia_id INTO v_disc;
  INSERT INTO operacion.reporte_faltante (linea_id, encontradas, faltantes, motivo, descripcion, picker_id, discrepancia_id, clave)
  VALUES (p_linea, p_encontradas, p_faltantes, p_motivo, p_descripcion, p_picker, v_disc, p_clave)
  RETURNING faltante_id INTO v_fal;
  PERFORM operacion._estado_existencia((x.p).tienda_id, (x.l).sku, p_corr);              -- EN_VERIFICACION: venta bloqueada
  PERFORM comun.publicar('M5', 'FaltanteReportado', (x.p).pedido_id::text, p_corr,
          jsonb_build_object('pedido_id', (x.p).pedido_id, 'tienda_id', (x.p).tienda_id, 'sku', (x.l).sku, 'faltante_id', v_fal,
                             'discrepancia_id', v_disc, 'faltantes', p_faltantes, 'motivo', p_motivo, 'picker_id', p_picker));
  PERFORM operacion._notificar('SUPERVISOR:tienda-' || (x.p).tienda_id, 'ALERTA_OPERATIVA',
          jsonb_build_object('evento', 'FALTANTE_POR_VALIDAR', 'faltante_id', v_fal, 'sku', (x.l).sku), 'faltante:' || v_fal, ARRAY['INTERNO']);
  v_res := jsonb_build_object('resultado', 'POR_VALIDAR', 'faltante_id', v_fal, 'discrepancia_id', v_disc,
                              'estado_pedido', operacion._evaluar_pedido((x.p).pedido_id, p_corr));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- API-OPS-04 ValidarFaltante (supervisor, INV-19)
CREATE FUNCTION operacion.validar_faltante(p_faltante uuid, p_supervisor uuid, p_valido boolean, p_evidencia text,
                                           p_clave text, p_corr uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; f operacion.reporte_faltante; x record; v_ahora timestamptz := clock_timestamp();
        q_vend int; q_comp int; v_mov bigint; v_monto numeric(12,2); v_res jsonb;
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'ValidarFaltante', encode(digest(p_faltante::text || ':' || p_valido, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  SELECT * INTO f FROM operacion.reporte_faltante WHERE faltante_id = p_faltante;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ENCONTRADO' USING ERRCODE = 'P0001'; END IF;
  IF f.estado <> 'POR_VALIDAR' THEN RAISE EXCEPTION 'FALTANTE_YA_RESUELTO' USING ERRCODE = 'P0001', DETAIL = f.estado; END IF;
  SELECT * INTO x FROM operacion._preparar_linea(f.linea_id);                              -- bloquea pedido, línea y existencia
  SELECT * INTO f FROM operacion.reporte_faltante WHERE faltante_id = p_faltante FOR UPDATE;
  IF f.estado <> 'POR_VALIDAR' THEN RAISE EXCEPTION 'FALTANTE_YA_RESUELTO' USING ERRCODE = 'P0001', DETAIL = f.estado; END IF;
  IF f.picker_id = p_supervisor THEN RAISE EXCEPTION 'VALIDADOR_IGUAL_A_PICKER' USING ERRCODE = 'P0001'; END IF;
  IF COALESCE(btrim(p_evidencia), '') = '' THEN RAISE EXCEPTION 'MOTIVO_REQUERIDO' USING ERRCODE = 'P0001', DETAIL = 'evidencia'; END IF;

  IF p_valido THEN
    q_vend := LEAST(f.faltantes, (x.e).vendible);                                         -- INV-02: nunca negativo
    q_comp := LEAST(f.faltantes, (x.e).comprometido);
    UPDATE inventario.existencia SET vendible = vendible - q_vend, comprometido = comprometido - q_comp, actualizado_en = v_ahora
     WHERE tienda_id = (x.p).tienda_id AND sku = (x.l).sku;
    v_mov := inventario._mov((x.p).tienda_id, (x.l).sku, 'FALTANTE', 'vendible', -q_vend, (x.e).vendible,
                             p_supervisor::text, 'PEDIDO', (x.p).pedido_id::text, p_corr, 'FALTANTE ' || f.motivo || ' · reporte ' || p_faltante);
    PERFORM inventario._mov((x.p).tienda_id, (x.l).sku, 'FALTANTE', 'comprometido', -q_comp, (x.e).comprometido,
                            p_supervisor::text, 'PEDIDO', (x.p).pedido_id::text, p_corr, 'FALTANTE ' || f.motivo || ' · reporte ' || p_faltante);
    UPDATE inventario.discrepancia SET estado = 'RESUELTA', resuelta_por_movimiento_id = v_mov, resuelta_en = v_ahora
     WHERE discrepancia_id = f.discrepancia_id;
    INSERT INTO pedidos.cancelacion_linea (linea_id, cantidad, causa, discrepancia_id, clave, actor)
    VALUES (f.linea_id, f.faltantes, 'FALTANTE_PICKING', f.discrepancia_id, p_faltante || '#K3', p_supervisor::text);
    UPDATE pedidos.linea_pedido SET cantidad_cancelada = cantidad_cancelada + f.faltantes WHERE linea_id = f.linea_id;
    UPDATE operacion.reporte_faltante SET estado = 'VALIDADO', supervisor_id = p_supervisor, evidencia = p_evidencia,
                                          validado_en = v_ahora WHERE faltante_id = p_faltante;
    -- PD05: solo la mercancía faltante, al precio fijado en el checkout; K4 determinista → un solo reembolso
    v_monto := f.faltantes * (x.l).precio_unitario;
    PERFORM comun.publicar('M2', 'SolicitudAjusteCobro', (x.p).pedido_id::text, p_corr,
            jsonb_build_object('pedido_id', (x.p).pedido_id, 'componente', 'MERCANCIA', 'tipo', 'PARCIAL', 'monto', v_monto::text,
                               'motivo', 'FALTANTE_PICKING', 'clave', p_faltante || '#K4', 'linea_id', f.linea_id, 'sku', (x.l).sku));
    PERFORM operacion._notificar((x.p).cliente_id::text, 'CANCELACION_PARCIAL',
            jsonb_build_object('pedido_id', (x.p).pedido_id, 'sku', (x.l).sku, 'cantidad', f.faltantes, 'reembolso', v_monto::text),
            'cancelacion_parcial:' || p_faltante, ARRAY['APP','CORREO']);
    INSERT INTO operacion.tarea_conteo (tienda_id, sku, tipo, disparador, nivel_riesgo, motivo)
    VALUES ((x.p).tienda_id, (x.l).sku, 'DIRIGIDO', 'FALTANTE_VALIDADO', 'ALTO', 'Confirmar existencia tras faltante ' || p_faltante);
    PERFORM comun.publicar('M5', 'FaltanteValidado', (x.p).pedido_id::text, p_corr,
            jsonb_build_object('pedido_id', (x.p).pedido_id, 'tienda_id', (x.p).tienda_id, 'sku', (x.l).sku, 'faltante_id', p_faltante,
                               'supervisor_id', p_supervisor, 'cantidad_cancelada', f.faltantes, 'reembolso', v_monto::text, 'movimiento_id', v_mov));
  ELSE
    -- El supervisor no confirma el faltante: la línea vuelve a estar pendiente de surtir; la discrepancia sigue abierta
    -- (venta bloqueada) hasta que un conteo dirigido la resuelva.
    UPDATE operacion.reporte_faltante SET estado = 'NO_VALIDADO', supervisor_id = p_supervisor, evidencia = p_evidencia,
                                          validado_en = v_ahora WHERE faltante_id = p_faltante;
    INSERT INTO operacion.tarea_conteo (tienda_id, sku, tipo, disparador, nivel_riesgo, motivo)
    VALUES ((x.p).tienda_id, (x.l).sku, 'DIRIGIDO', 'FALTANTE_NO_VALIDADO', 'ALTO', 'Resolver discrepancia del reporte ' || p_faltante);
    PERFORM comun.publicar('M5', 'FaltanteNoValidado', (x.p).pedido_id::text, p_corr,
            jsonb_build_object('pedido_id', (x.p).pedido_id, 'sku', (x.l).sku, 'faltante_id', p_faltante, 'supervisor_id', p_supervisor));
  END IF;
  PERFORM operacion._estado_existencia((x.p).tienda_id, (x.l).sku, p_corr);
  v_res := jsonb_build_object('resultado', CASE WHEN p_valido THEN 'VALIDADO' ELSE 'NO_VALIDADO' END, 'faltante_id', p_faltante,
                              'reembolso_solicitado', CASE WHEN p_valido THEN v_monto::text END,
                              'estado_pedido', operacion._evaluar_pedido((x.p).pedido_id, p_corr));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- API-OPS-05 RegistrarConteo (AT-07)
CREATE FUNCTION operacion.registrar_conteo(p_tienda smallint, p_sku text, p_cantidad_fisica int, p_responsable uuid,
                                           p_clave text, p_corr uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; e inventario.existencia; v_ahora timestamptz := clock_timestamp(); v_tarea uuid; v_mov bigint;
        v_disc uuid; v_ajuste uuid; v_res jsonb;
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'RegistrarConteo',
            encode(digest(p_tienda || ':' || p_sku || ':' || p_cantidad_fisica, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  IF p_cantidad_fisica < 0 THEN RAISE EXCEPTION 'CANTIDAD_INVALIDA' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = p_tienda AND sku = p_sku FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ENCONTRADO' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM operacion.ajuste_inventario WHERE tienda_id = p_tienda AND sku = p_sku AND estado = 'PENDIENTE_AUTORIZACION') THEN
    RAISE EXCEPTION 'AJUSTE_PENDIENTE' USING ERRCODE = 'P0001';
  END IF;
  -- Toma la tarea dirigida o cíclica abierta más antigua del SKU; si no hay, registra un conteo no programado
  SELECT tarea_id INTO v_tarea FROM operacion.tarea_conteo
   WHERE tienda_id = p_tienda AND sku = p_sku AND estado IN ('PROGRAMADA','EN_CURSO') ORDER BY creada_en LIMIT 1 FOR UPDATE;
  IF v_tarea IS NULL THEN
    INSERT INTO operacion.tarea_conteo (tienda_id, sku, tipo, disparador, nivel_riesgo, motivo)
    VALUES (p_tienda, p_sku, 'CICLICO', 'CONTEO_NO_PROGRAMADO', 'MEDIO', 'Registrado por el responsable') RETURNING tarea_id INTO v_tarea;
  END IF;
  UPDATE operacion.tarea_conteo SET responsable_id = p_responsable, inicio = COALESCE(inicio, v_ahora), fin = v_ahora,
                                    cantidad_fisica = p_cantidad_fisica,
                                    estado = CASE WHEN p_cantidad_fisica = e.vendible THEN 'CERRADA' ELSE 'REGISTRADA' END
   WHERE tarea_id = v_tarea;
  IF p_cantidad_fisica = e.vendible THEN
    -- Sin diferencia: queda constancia (movimiento de delta 0) y se cierran las discrepancias abiertas del SKU
    v_mov := inventario._mov(p_tienda, p_sku, 'CONTEO', 'vendible', 0, e.vendible, p_responsable::text, 'CONTEO', v_tarea::text, p_corr,
                             'CONTEO_SIN_DIFERENCIA');
    UPDATE inventario.discrepancia SET estado = 'RESUELTA', resuelta_por_movimiento_id = v_mov, resuelta_en = v_ahora
     WHERE tienda_id = p_tienda AND sku = p_sku AND estado = 'ABIERTA';
    v_res := jsonb_build_object('resultado', 'SIN_DIFERENCIA', 'tarea_id', v_tarea, 'cantidad_logica', e.vendible);
  ELSE
    INSERT INTO inventario.discrepancia (tienda_id, sku, origen, cantidad_logica, cantidad_fisica)
    VALUES (p_tienda, p_sku, 'CONTEO', e.vendible, p_cantidad_fisica) RETURNING discrepancia_id INTO v_disc;
    INSERT INTO operacion.ajuste_inventario (tienda_id, sku, tipo, saldo_afectado, cantidad, motivo, discrepancia_id, solicitante_id,
                                             estado, clave)
    VALUES (p_tienda, p_sku, CASE WHEN p_cantidad_fisica < e.vendible THEN 'AJUSTE_NEG' ELSE 'AJUSTE_POS' END, 'vendible',
            abs(p_cantidad_fisica - e.vendible), 'CONTEO_FISICO', v_disc, p_responsable, 'PENDIENTE_AUTORIZACION', p_clave || '#K5')
    RETURNING ajuste_inv_id INTO v_ajuste;
    PERFORM comun.publicar('M5', 'DiscrepanciaDetectada', p_tienda || ':' || p_sku, p_corr,
            jsonb_build_object('tienda_id', p_tienda, 'sku', p_sku, 'discrepancia_id', v_disc, 'cantidad_logica', e.vendible,
                               'cantidad_fisica', p_cantidad_fisica, 'ajuste_id', v_ajuste, 'responsable_id', p_responsable));
    PERFORM operacion._notificar('SUPERVISOR:tienda-' || p_tienda, 'ALERTA_OPERATIVA',
            jsonb_build_object('evento', 'AJUSTE_POR_AUTORIZAR', 'ajuste_id', v_ajuste, 'sku', p_sku), 'ajuste:' || v_ajuste, ARRAY['INTERNO']);
    v_res := jsonb_build_object('resultado', 'DISCREPANCIA', 'tarea_id', v_tarea, 'discrepancia_id', v_disc, 'ajuste_id', v_ajuste,
                                'cantidad_logica', e.vendible, 'cantidad_fisica', p_cantidad_fisica, 'diferencia', p_cantidad_fisica - e.vendible);
  END IF;
  v_res := v_res || jsonb_build_object('estado_existencia', operacion._estado_existencia(p_tienda, p_sku, p_corr));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- API-OPS-06 AutorizarAjuste (INV-14: otra persona)
CREATE FUNCTION operacion.autorizar_ajuste(p_ajuste uuid, p_autorizador uuid, p_aprobar boolean, p_evidencia text,
                                           p_clave text, p_corr uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; a operacion.ajuste_inventario; e inventario.existencia; v_ahora timestamptz := clock_timestamp();
        v_delta int; v_mov bigint; v_res jsonb;
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'AutorizarAjuste', encode(digest(p_ajuste::text || ':' || p_aprobar, 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  SELECT * INTO a FROM operacion.ajuste_inventario WHERE ajuste_inv_id = p_ajuste;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ENCONTRADO' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = a.tienda_id AND sku = a.sku FOR UPDATE;
  SELECT * INTO a FROM operacion.ajuste_inventario WHERE ajuste_inv_id = p_ajuste FOR UPDATE;
  IF a.estado <> 'PENDIENTE_AUTORIZACION' THEN RAISE EXCEPTION 'AJUSTE_YA_RESUELTO' USING ERRCODE = 'P0001', DETAIL = a.estado; END IF;
  IF a.solicitante_id = p_autorizador THEN RAISE EXCEPTION 'AUTORIZADOR_IGUAL_A_SOLICITANTE' USING ERRCODE = 'P0001'; END IF;
  IF COALESCE(btrim(p_evidencia), '') = '' THEN RAISE EXCEPTION 'MOTIVO_REQUERIDO' USING ERRCODE = 'P0001', DETAIL = 'evidencia'; END IF;
  IF p_aprobar THEN
    v_delta := CASE WHEN a.tipo = 'AJUSTE_POS' THEN a.cantidad ELSE -LEAST(a.cantidad, e.vendible) END;
    UPDATE inventario.existencia SET vendible = vendible + v_delta, actualizado_en = v_ahora WHERE tienda_id = a.tienda_id AND sku = a.sku;
    v_mov := inventario._mov(a.tienda_id, a.sku, a.tipo, 'vendible', v_delta, e.vendible, p_autorizador::text,
                             'AJUSTE_INVENTARIO', p_ajuste::text, p_corr, a.motivo || ' · solicitó ' || a.solicitante_id);
    UPDATE operacion.ajuste_inventario SET estado = 'APLICADO', autorizador_id = p_autorizador, evidencia_inspeccion = p_evidencia,
                                           movimiento_id = v_mov WHERE ajuste_inv_id = p_ajuste;
    UPDATE inventario.discrepancia SET estado = 'RESUELTA', resuelta_por_movimiento_id = v_mov, resuelta_en = v_ahora
     WHERE tienda_id = a.tienda_id AND sku = a.sku AND estado = 'ABIERTA';
    UPDATE operacion.tarea_conteo SET estado = 'CERRADA' WHERE tienda_id = a.tienda_id AND sku = a.sku AND estado = 'REGISTRADA';
    PERFORM comun.publicar('M5', 'AjusteInventarioAplicado', a.tienda_id || ':' || a.sku, p_corr,
            jsonb_build_object('tienda_id', a.tienda_id, 'sku', a.sku, 'ajuste_id', p_ajuste, 'delta', v_delta, 'movimiento_id', v_mov,
                               'solicitante_id', a.solicitante_id, 'autorizador_id', p_autorizador));
    v_res := jsonb_build_object('resultado', 'APLICADO', 'ajuste_id', p_ajuste, 'delta', v_delta, 'movimiento_id', v_mov,
                                'vendible', e.vendible + v_delta);
  ELSE
    UPDATE operacion.ajuste_inventario SET estado = 'RECHAZADO', autorizador_id = p_autorizador, evidencia_inspeccion = p_evidencia
     WHERE ajuste_inv_id = p_ajuste;
    INSERT INTO operacion.tarea_conteo (tienda_id, sku, tipo, disparador, nivel_riesgo, motivo)
    VALUES (a.tienda_id, a.sku, 'DIRIGIDO', 'AJUSTE_RECHAZADO', 'ALTO', 'Reconteo: ajuste ' || p_ajuste || ' rechazado');
    PERFORM comun.publicar('M5', 'AjusteInventarioRechazado', a.tienda_id || ':' || a.sku, p_corr,
            jsonb_build_object('tienda_id', a.tienda_id, 'sku', a.sku, 'ajuste_id', p_ajuste, 'autorizador_id', p_autorizador));
    v_res := jsonb_build_object('resultado', 'RECHAZADO', 'ajuste_id', p_ajuste);
  END IF;
  v_res := v_res || jsonb_build_object('estado_existencia', operacion._estado_existencia(a.tienda_id, a.sku, p_corr));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;
