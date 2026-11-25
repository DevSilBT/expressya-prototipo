-- 008 · Respuesta a la retroalimentación del 24-nov-2026 (caso «lógico 3, físico 2»)
-- 1) RegistrarSurtido: el picker declara cuántas unidades ENCONTRÓ de las pendientes; en UNA transacción se surte lo
--    encontrado (ADR-02) y la diferencia se reporta como faltante (discrepancia + SKU en verificación, ADR-27).
--    La cancelación y el reembolso los decide después el supervisor, que debe ser otra persona (INV-19, ADR-28).
-- 2) Quién operó: nombre visible de las sesiones simuladas para la bitácora del personal (en producción lo dará el
--    proveedor de identidad, ADR-25; la sesión simulada responde 404 con AMBIENTE=produccion).
-- 3) Reposición auditada de un escenario de demostración (solo la usa un endpoint que no existe en producción).

CREATE TABLE IF NOT EXISTS acceso.sesion_demo (
  usuario_id     uuid PRIMARY KEY,
  nombre         text NOT NULL CHECK (length(nombre) BETWEEN 2 AND 80),
  rol            text NOT NULL,
  tienda_id      smallint,
  actualizado_en timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- ---------------------------------------------------------------- API-OPS-07 RegistrarSurtido (picker)
CREATE FUNCTION operacion.registrar_surtido(p_linea uuid, p_encontradas int, p_motivo text, p_descripcion text,
                                            p_picker uuid, p_clave text, p_corr uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_prev jsonb; x record; v_falt int; r_s jsonb; r_f jsonb; v_res jsonb;
BEGIN
  v_prev := comun.reclamar_clave(p_clave, 'RegistrarSurtido',
            encode(digest(p_linea::text || ':' || p_encontradas || ':' || COALESCE(p_motivo, ''), 'sha256'), 'hex'));
  IF v_prev IS NOT NULL THEN RETURN v_prev; END IF;
  SELECT * INTO x FROM operacion._preparar_linea(p_linea);          -- bloquea pedido → línea → existencia; exige EN_PREPARACION
  IF x.pendiente <= 0 THEN
    RAISE EXCEPTION 'CANTIDAD_INVALIDA' USING ERRCODE = 'P0001', DETAIL = 'No quedan unidades por surtir en esta línea';
  END IF;
  IF p_encontradas IS NULL OR p_encontradas < 0 OR p_encontradas > x.pendiente THEN
    RAISE EXCEPTION 'CANTIDAD_INVALIDA' USING ERRCODE = 'P0001',
      DETAIL = 'Encontradas debe estar entre 0 y ' || x.pendiente || ' (pendientes de surtir)';
  END IF;
  v_falt := x.pendiente - p_encontradas;
  IF v_falt > 0 AND p_motivo IS NULL THEN
    RAISE EXCEPTION 'MOTIVO_REQUERIDO' USING ERRCODE = 'P0001', DETAIL = 'Indica el motivo del faltante';
  END IF;
  IF p_encontradas > 0 THEN
    r_s := operacion.surtir_linea(p_linea, p_encontradas, p_picker, p_clave || '#S', p_corr);
  END IF;
  IF v_falt > 0 THEN
    r_f := operacion.reportar_faltante(p_linea, p_encontradas, v_falt, p_motivo, p_descripcion, p_picker, p_clave || '#F', p_corr);
  END IF;
  v_res := jsonb_build_object(
    'resultado', CASE WHEN v_falt > 0 THEN 'SURTIDO_CON_FALTANTE' ELSE 'SURTIDA' END,
    'linea_id', p_linea, 'sku', (x.l).sku, 'pendiente_antes', x.pendiente,
    'surtidas', p_encontradas, 'faltantes', v_falt, 'faltante_id', r_f->>'faltante_id',
    'estado_pedido', COALESCE(r_f->>'estado_pedido', r_s->>'estado_pedido'));
  RETURN comun.guardar_resultado(p_clave, v_res);
END $$;

-- ---------------------------------------------------------------- Reposición auditada de un escenario de demostración
-- Solo SUBE el saldo vendible hasta el objetivo con un movimiento (INV-08) y únicamente si la existencia está libre:
-- sin unidades comprometidas, sin reservas vigentes y sin discrepancia abierta. Nunca borra ni reescribe historial.
CREATE FUNCTION inventario.preparar_escenario(p_tienda smallint, p_sku text, p_objetivo int, p_actor text) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE e inventario.existencia; v_res int; v_agregadas int := 0;
BEGIN
  SELECT * INTO e FROM inventario.existencia WHERE tienda_id = p_tienda AND sku = p_sku FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ENCONTRADO' USING ERRCODE = 'P0001', DETAIL = 'Existencia inexistente'; END IF;
  v_res := inventario.reservas_vigentes(p_tienda, p_sku, clock_timestamp());
  IF e.comprometido > 0 OR v_res > 0 OR e.estado <> 'NORMAL' OR e.vendible > p_objetivo THEN
    RAISE EXCEPTION 'ESCENARIO_OCUPADO' USING ERRCODE = 'P0001',
      DETAIL = format('vendible %s, comprometido %s, reservado %s, estado %s: termina o valida el caso anterior',
                      e.vendible, e.comprometido, v_res, e.estado);
  END IF;
  IF e.vendible < p_objetivo THEN
    v_agregadas := p_objetivo - e.vendible;
    PERFORM inventario.cargar_existencia(p_tienda, p_sku, v_agregadas, p_actor,
            'Reposición del escenario de demostración: inventario lógico ' || p_objetivo);
  END IF;
  RETURN jsonb_build_object('tienda_id', p_tienda, 'sku', p_sku, 'vendible', p_objetivo, 'agregadas', v_agregadas);
END $$;
