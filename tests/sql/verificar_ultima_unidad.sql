\set ON_ERROR_STOP 1
DO $$
DECLARE n_ok int; n_sol int; n_rech int; v_disp int;
BEGIN
  SELECT count(*) INTO n_ok  FROM inventario.reserva r JOIN inventario.reserva_linea l USING (reserva_id)
   WHERE l.tienda_id = 1 AND l.sku = 'SKU-X' AND r.estado = 'ACTIVA';
  SELECT count(*), count(*) FILTER (WHERE resultado = 'AGOTADO') INTO n_sol, n_rech FROM inventario.solicitud_reserva WHERE tienda_id = 1;
  v_disp := inventario.disponible(1::smallint, 'SKU-X');
  RAISE NOTICE 'TP-S3-04 · solicitudes=% aceptadas=% agotado=% disponible=%', n_sol, n_ok, n_rech, v_disp;
  IF n_ok <> 1 OR n_sol <> 100 OR n_rech <> 99 OR v_disp <> 0 THEN RAISE EXCEPTION 'FALLA TP-S3-04'; END IF;
  RAISE NOTICE 'TP-S3-04 OK: exactamente una reserva, 99 rechazos registrados, disponibilidad 0 (nunca negativa)';
END $$;
