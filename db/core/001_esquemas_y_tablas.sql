-- ExpressYa · Equipo 02 · Dinamita · Modelo físico (PostgreSQL 16) · Base de datos del monolito modular
-- Esquema por módulo lógico (ADR-11): cada módulo es dueño de su esquema.
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid(), digest()

CREATE SCHEMA IF NOT EXISTS comun;          -- idempotencia y bandeja de salida (ADR-05, ADR-12)
CREATE SCHEMA IF NOT EXISTS acceso;         -- C0
CREATE SCHEMA IF NOT EXISTS catalogo;       -- M1
CREATE SCHEMA IF NOT EXISTS pedidos;        -- M2
CREATE SCHEMA IF NOT EXISTS inventario;     -- M3 (autoridad)
CREATE SCHEMA IF NOT EXISTS operacion;      -- M5
CREATE SCHEMA IF NOT EXISTS notificaciones; -- M6
CREATE SCHEMA IF NOT EXISTS trazabilidad;   -- M7

-- ---------------------------------------------------------------- comun
CREATE TABLE comun.operacion_procesada (
  clave        text PRIMARY KEY,
  tipo         text NOT NULL,
  huella       text NOT NULL,                       -- sha256 de la solicitud normalizada
  resultado    jsonb,                               -- NULL mientras la transacción está en curso
  creada_en    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE comun.outbox (
  evento_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  modulo        text NOT NULL,
  tipo          text NOT NULL,
  agregado_id   text NOT NULL,
  correlacion_id uuid,
  datos         jsonb NOT NULL,
  creado_en     timestamptz NOT NULL DEFAULT clock_timestamp(),
  entregado_en  timestamptz,
  intentos      int NOT NULL DEFAULT 0 CHECK (intentos >= 0)
);
CREATE INDEX outbox_pendientes ON comun.outbox (creado_en) WHERE entregado_en IS NULL;

-- ---------------------------------------------------------------- acceso (C0)
CREATE TABLE acceso.rol (
  rol text PRIMARY KEY CHECK (rol IN ('CLIENTE','PICKER','SUPERVISOR','ENCARGADO','RECEPCION','FINANZAS','ATENCION','AUDITOR','SERVICIO'))
);
CREATE TABLE acceso.usuario (
  usuario_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  correo      text NOT NULL UNIQUE,
  hash_clave  text NOT NULL,
  tienda_id   smallint,                             -- personal operativo ligado a una tienda
  activo      boolean NOT NULL DEFAULT true,
  intentos_fallidos smallint NOT NULL DEFAULT 0 CHECK (intentos_fallidos >= 0),
  creado_en   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE acceso.usuario_rol (
  usuario_id uuid NOT NULL REFERENCES acceso.usuario,
  rol        text NOT NULL REFERENCES acceso.rol,
  PRIMARY KEY (usuario_id, rol)
);

-- ---------------------------------------------------------------- catálogo (M1)
CREATE TABLE catalogo.producto (
  sku          text PRIMARY KEY CHECK (sku ~ '^[A-Z0-9-]{3,40}$'),
  descripcion  text NOT NULL,
  precio       numeric(12,2) NOT NULL CHECK (precio >= 0),
  activo       boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------- inventario (M3)
CREATE TABLE inventario.dark_store (
  tienda_id  smallint PRIMARY KEY,
  nombre     text NOT NULL UNIQUE,
  activa     boolean NOT NULL DEFAULT true
);

CREATE TABLE inventario.existencia (
  tienda_id     smallint NOT NULL REFERENCES inventario.dark_store,
  sku           text     NOT NULL REFERENCES catalogo.producto,
  vendible      integer  NOT NULL DEFAULT 0 CHECK (vendible >= 0),       -- INV-02
  comprometido  integer  NOT NULL DEFAULT 0 CHECK (comprometido >= 0),   -- ADR-02
  en_recepcion  integer  NOT NULL DEFAULT 0 CHECK (en_recepcion >= 0),
  en_revision   integer  NOT NULL DEFAULT 0 CHECK (en_revision >= 0),
  estado        text     NOT NULL DEFAULT 'NORMAL'
                CHECK (estado IN ('NORMAL','SOBRECOMPROMETIDA','EN_VERIFICACION')),
  actualizado_en timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tienda_id, sku)
);

-- Registro de TODA solicitud de reserva, aceptada o no (RN09, EVT-03)
CREATE TABLE inventario.solicitud_reserva (
  solicitud_seq  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pedido_id      uuid NOT NULL,
  tienda_id      smallint NOT NULL,
  clave          text NOT NULL,
  registrada_en  timestamptz NOT NULL DEFAULT clock_timestamp(),
  resultado      text CHECK (resultado IN ('ACEPTADA','AGOTADO','SKU_BLOQUEADO'))
);

CREATE TABLE inventario.reserva (
  reserva_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pedido_id      uuid NOT NULL,
  tienda_id      smallint NOT NULL REFERENCES inventario.dark_store,
  estado         text NOT NULL DEFAULT 'ACTIVA'
                 CHECK (estado IN ('ACTIVA','CONFIRMADA','LIBERADA','EXPIRADA','LIBERADA_AGOTAMIENTO')),
  creada_en      timestamptz NOT NULL,
  expira_en      timestamptz NOT NULL,
  cerrada_en     timestamptz,
  motivo_cierre  text,
  clave          text NOT NULL UNIQUE,              -- K1
  solicitud_seq  bigint NOT NULL REFERENCES inventario.solicitud_reserva,
  CONSTRAINT reserva_300s CHECK (expira_en = creada_en + interval '300 seconds'),   -- Cliente 1-nov (12)
  CONSTRAINT reserva_cierre CHECK ((estado = 'ACTIVA') = (cerrada_en IS NULL))
);
CREATE UNIQUE INDEX reserva_una_activa_por_pedido ON inventario.reserva (pedido_id) WHERE estado = 'ACTIVA';
CREATE INDEX reserva_activas_por_vencer ON inventario.reserva (expira_en) WHERE estado = 'ACTIVA';

CREATE TABLE inventario.reserva_linea (
  reserva_id  uuid NOT NULL REFERENCES inventario.reserva ON DELETE RESTRICT,
  linea_id    uuid NOT NULL,
  tienda_id   smallint NOT NULL,
  sku         text NOT NULL,
  cantidad    integer NOT NULL CHECK (cantidad > 0),
  vigente     boolean NOT NULL DEFAULT true,        -- copia desnormalizada para el índice parcial
  expira_en   timestamptz NOT NULL,
  PRIMARY KEY (reserva_id, linea_id),
  FOREIGN KEY (tienda_id, sku) REFERENCES inventario.existencia
);
CREATE INDEX reserva_linea_vigentes ON inventario.reserva_linea (tienda_id, sku, expira_en) WHERE vigente;

CREATE TABLE inventario.movimiento (
  movimiento_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tienda_id       smallint NOT NULL,
  sku             text NOT NULL,
  tipo            text NOT NULL CHECK (tipo IN ('RESERVA','LIBERACION','EXPIRACION','LIBERACION_AGOTAMIENTO',
                     'COMPROMISO','LIBERACION_COMPROMISO','SURTIDO','FALTANTE','MERMA','DANO','AJUSTE_POS','AJUSTE_NEG',
                     'ENTRADA_RECEPCION','VALIDACION_RECEPCION','DEVOLUCION_RECIBIDA','REINGRESO','MERMA_REVISION','CONTEO')),
  saldo           text NOT NULL CHECK (saldo IN ('vendible','comprometido','en_recepcion','en_revision','reservas_vigentes','ninguno')),
  delta           integer NOT NULL,
  saldo_antes     integer NOT NULL,
  saldo_despues   integer NOT NULL,
  motivo          text,
  actor           text NOT NULL,                     -- usuario_id o proceso (BARRIDO, SISTEMA…)
  doc_tipo        text,
  doc_id          text,
  correlacion_id  uuid,
  registrado_en   timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT movimiento_aritmetica CHECK (saldo_despues = saldo_antes + delta),        -- DT-09
  FOREIGN KEY (tienda_id, sku) REFERENCES inventario.existencia
);
CREATE INDEX movimiento_historial ON inventario.movimiento (tienda_id, sku, registrado_en, movimiento_id);

CREATE TABLE inventario.discrepancia (
  discrepancia_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tienda_id        smallint NOT NULL,
  sku              text NOT NULL,
  origen           text NOT NULL CHECK (origen IN ('FALTANTE','CONTEO','RECEPCION')),
  cantidad_logica  integer NOT NULL CHECK (cantidad_logica >= 0),
  cantidad_fisica  integer NOT NULL CHECK (cantidad_fisica >= 0),
  diferencia       integer GENERATED ALWAYS AS (cantidad_fisica - cantidad_logica) STORED,
  estado           text NOT NULL DEFAULT 'ABIERTA' CHECK (estado IN ('ABIERTA','RESUELTA')),
  resuelta_por_movimiento_id bigint REFERENCES inventario.movimiento,
  creada_en        timestamptz NOT NULL DEFAULT clock_timestamp(),
  resuelta_en      timestamptz,
  CONSTRAINT discrepancia_resuelta CHECK ((estado = 'RESUELTA') = (resuelta_por_movimiento_id IS NOT NULL)),  -- INV-18
  FOREIGN KEY (tienda_id, sku) REFERENCES inventario.existencia
);

CREATE TABLE inventario.sobrecompromiso (
  sobrecompromiso_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tienda_id   smallint NOT NULL,
  sku         text NOT NULL,
  deficit     integer NOT NULL CHECK (deficit > 0),
  movimiento_causa_id bigint NOT NULL REFERENCES inventario.movimiento,
  estado      text NOT NULL DEFAULT 'ABIERTO' CHECK (estado IN ('ABIERTO','RESUELTO')),
  creado_en   timestamptz NOT NULL DEFAULT clock_timestamp(),
  resuelto_en timestamptz,
  FOREIGN KEY (tienda_id, sku) REFERENCES inventario.existencia
);
CREATE UNIQUE INDEX sobrecompromiso_uno_abierto ON inventario.sobrecompromiso (tienda_id, sku) WHERE estado = 'ABIERTO';

-- Historial de solo agregado (ADR-06, INV-08, INV-18): se impiden UPDATE/DELETE
CREATE FUNCTION comun.impedir_modificacion() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'HISTORIAL_INMUTABLE: % no admite %', TG_TABLE_NAME, TG_OP USING ERRCODE = 'P0001';
END $$;
CREATE TRIGGER movimiento_inmutable BEFORE UPDATE OR DELETE ON inventario.movimiento
  FOR EACH ROW EXECUTE FUNCTION comun.impedir_modificacion();

-- En una discrepancia solo cambian estado, resuelta_por y resuelta_en (las cantidades son inmutables)
CREATE FUNCTION inventario.proteger_discrepancia() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' OR NEW.cantidad_logica <> OLD.cantidad_logica OR NEW.cantidad_fisica <> OLD.cantidad_fisica
     OR NEW.origen <> OLD.origen OR NEW.tienda_id <> OLD.tienda_id OR NEW.sku <> OLD.sku OR OLD.estado = 'RESUELTA' THEN
    RAISE EXCEPTION 'HISTORIAL_INMUTABLE: discrepancia %', OLD.discrepancia_id USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER discrepancia_inmutable BEFORE UPDATE OR DELETE ON inventario.discrepancia
  FOR EACH ROW EXECUTE FUNCTION inventario.proteger_discrepancia();

-- ---------------------------------------------------------------- pedidos (M2)
CREATE TABLE pedidos.zona_cobertura (
  tienda_id      smallint NOT NULL REFERENCES inventario.dark_store,
  codigo_postal  text NOT NULL CHECK (codigo_postal ~ '^[0-9]{5}$'),
  distancia_km   numeric(6,2) NOT NULL CHECK (distancia_km >= 0),   -- cercanía configurada (ADR-13)
  activa         boolean NOT NULL DEFAULT true,
  PRIMARY KEY (tienda_id, codigo_postal)                              -- se permite superposición entre tiendas
);
CREATE INDEX zona_por_cp ON pedidos.zona_cobertura (codigo_postal) WHERE activa;

CREATE TABLE pedidos.pedido (
  pedido_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cliente_id          uuid NOT NULL,
  tienda_id           smallint NOT NULL REFERENCES inventario.dark_store,
  codigo_postal       text NOT NULL,
  estado              text NOT NULL CHECK (estado IN ('EN_CHECKOUT','ESPERANDO_DECISION','RESERVADO','PAGO_SOLICITADO',
                        'PAGO_INCIERTO','PAGADO','EN_PREPARACION','COMPLETO','PARCIAL','CANCELADO_TOTAL','NO_PAGADO',
                        'ABANDONADO','PAGO_SIN_EXISTENCIA','REEMBOLSADO','CANCELADO_POR_CLIENTE')),
  subtotal_mercancia  numeric(12,2) NOT NULL CHECK (subtotal_mercancia >= 0),   -- PD05: componentes separados
  envio               numeric(12,2) NOT NULL DEFAULT 0 CHECK (envio >= 0),
  propina             numeric(12,2) NOT NULL DEFAULT 0 CHECK (propina >= 0),
  descuento_promocion numeric(12,2) NOT NULL DEFAULT 0 CHECK (descuento_promocion >= 0),
  total               numeric(12,2) GENERATED ALWAYS AS (subtotal_mercancia + envio + propina - descuento_promocion) STORED,
  hora_confirmacion   timestamptz,                    -- prioridad entre pedidos pagados (ADR-14)
  clave_checkout      text NOT NULL UNIQUE,           -- K1
  creado_en           timestamptz NOT NULL DEFAULT clock_timestamp(),
  actualizado_en      timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT total_no_negativo CHECK (subtotal_mercancia + envio + propina - descuento_promocion >= 0)
);
CREATE INDEX pedido_por_estado ON pedidos.pedido (estado, actualizado_en);

CREATE TABLE pedidos.linea_pedido (
  linea_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pedido_id           uuid NOT NULL REFERENCES pedidos.pedido ON DELETE RESTRICT,
  sku                 text NOT NULL REFERENCES catalogo.producto,
  cantidad_pedida     integer NOT NULL CHECK (cantidad_pedida > 0),
  precio_unitario     numeric(12,2) NOT NULL CHECK (precio_unitario >= 0),   -- fijado en el checkout (Cliente 1-nov, 21)
  cantidad_surtida    integer NOT NULL DEFAULT 0 CHECK (cantidad_surtida >= 0),
  cantidad_cancelada  integer NOT NULL DEFAULT 0 CHECK (cantidad_cancelada >= 0),
  estado              text NOT NULL DEFAULT 'PENDIENTE' CHECK (estado IN ('PENDIENTE','EXCLUIDA','RESERVADA','LIBERADA',
                        'COMPROMETIDA','FALTANTE_REPORTADO','SURTIDA','SURTIDA_PARCIAL','CANCELADA',
                        'CANCELADA_PAGO_SIN_EXISTENCIA','LIBERADA_POR_CANCELACION')),
  CONSTRAINT linea_inv16 CHECK (cantidad_surtida + cantidad_cancelada <= cantidad_pedida),      -- INV-16
  UNIQUE (pedido_id, sku)
);

CREATE TABLE pedidos.cancelacion_linea (
  cancelacion_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  linea_id        uuid NOT NULL REFERENCES pedidos.linea_pedido,
  cantidad        integer NOT NULL CHECK (cantidad > 0),
  causa           text NOT NULL CHECK (causa IN ('FALTANTE_PICKING','PAGO_SIN_EXISTENCIA','CANCELACION_CLIENTE')),
  discrepancia_id uuid,                               -- referencia lógica a inventario.discrepancia
  clave           text NOT NULL UNIQUE,               -- K3 o K4
  actor           text NOT NULL,
  registrada_en   timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT faltante_con_discrepancia CHECK (causa <> 'FALTANTE_PICKING' OR discrepancia_id IS NOT NULL)
);
CREATE TRIGGER cancelacion_inmutable BEFORE UPDATE OR DELETE ON pedidos.cancelacion_linea
  FOR EACH ROW EXECUTE FUNCTION comun.impedir_modificacion();

-- ---------------------------------------------------------------- operación (M5)
CREATE TABLE operacion.reporte_faltante (
  faltante_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  linea_id        uuid NOT NULL REFERENCES pedidos.linea_pedido,
  encontradas     integer NOT NULL CHECK (encontradas >= 0),
  faltantes       integer NOT NULL CHECK (faltantes > 0),
  motivo          text NOT NULL CHECK (motivo IN ('NO_ENCONTRADO','DANADO_EN_ANAQUEL','CADUCADO','UBICACION_INCORRECTA','OTRO')),
  descripcion     text,
  picker_id       uuid NOT NULL,
  discrepancia_id uuid NOT NULL,
  estado          text NOT NULL DEFAULT 'POR_VALIDAR' CHECK (estado IN ('POR_VALIDAR','VALIDADO','NO_VALIDADO')),
  supervisor_id   uuid,
  evidencia       text,
  validado_en     timestamptz,
  clave           text NOT NULL UNIQUE,              -- K3
  creado_en       timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT validacion_completa CHECK ((estado = 'POR_VALIDAR') = (supervisor_id IS NULL)),
  CONSTRAINT supervisor_distinto CHECK (supervisor_id IS NULL OR supervisor_id <> picker_id),
  CONSTRAINT otro_con_descripcion CHECK (motivo <> 'OTRO' OR descripcion IS NOT NULL)
);

CREATE TABLE operacion.ajuste_inventario (
  ajuste_inv_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tienda_id      smallint NOT NULL,
  sku            text NOT NULL,
  tipo           text NOT NULL CHECK (tipo IN ('MERMA','DANO','AJUSTE_POS','AJUSTE_NEG','REINCORPORACION')),
  saldo_afectado text NOT NULL CHECK (saldo_afectado IN ('vendible','en_recepcion','en_revision')),
  cantidad       integer NOT NULL CHECK (cantidad > 0),
  motivo         text NOT NULL,
  descripcion    text,
  discrepancia_id uuid,
  solicitante_id uuid NOT NULL,
  autorizador_id uuid,
  evidencia_inspeccion text,
  estado         text NOT NULL CHECK (estado IN ('SOLICITADO','PENDIENTE_AUTORIZACION','APLICADO','RECHAZADO')),
  umbral_aplicado integer,                            -- [PV] parámetro de configuración
  movimiento_id  bigint,
  clave          text NOT NULL UNIQUE,                -- K5
  creado_en      timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT autorizador_distinto CHECK (autorizador_id IS NULL OR autorizador_id <> solicitante_id),
  CONSTRAINT reincorporacion_doble CHECK (tipo <> 'REINCORPORACION' OR estado <> 'APLICADO'
                                          OR (autorizador_id IS NOT NULL AND evidencia_inspeccion IS NOT NULL)),  -- Cliente 1-nov (18)
  CONSTRAINT aplicado_con_movimiento CHECK ((estado = 'APLICADO') = (movimiento_id IS NOT NULL)),
  FOREIGN KEY (tienda_id, sku) REFERENCES inventario.existencia
);

CREATE TABLE operacion.tarea_conteo (
  tarea_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tienda_id       smallint NOT NULL,
  sku             text NOT NULL,
  tipo            text NOT NULL CHECK (tipo IN ('CICLICO','DIRIGIDO')),
  disparador      text NOT NULL,
  nivel_riesgo    text NOT NULL CHECK (nivel_riesgo IN ('ALTO','MEDIO','BAJO')),
  responsable_id  uuid,
  motivo          text,
  evidencia       text,
  inicio          timestamptz,
  fin             timestamptz,
  duracion        interval GENERATED ALWAYS AS (fin - inicio) STORED,     -- V-11: duración real
  cantidad_fisica integer CHECK (cantidad_fisica >= 0),
  estado          text NOT NULL DEFAULT 'PROGRAMADA' CHECK (estado IN ('PROGRAMADA','EN_CURSO','REGISTRADA','CERRADA')),
  alerta_30min_en timestamptz,
  creada_en       timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fin_despues_de_inicio CHECK (fin IS NULL OR (inicio IS NOT NULL AND fin >= inicio)),
  FOREIGN KEY (tienda_id, sku) REFERENCES inventario.existencia
);

-- ---------------------------------------------------------------- notificaciones (M6)
CREATE TABLE notificaciones.notificacion (
  notificacion_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  destinatario    text NOT NULL,
  tipo            text NOT NULL CHECK (tipo IN ('PAGO_RECHAZADO','PEDIDO_CONFIRMADO','CANCELACION_TOTAL','CANCELACION_PARCIAL',
                    'REEMBOLSO_CONFIRMADO','ALERTA_OPERATIVA')),
  canal           text NOT NULL CHECK (canal IN ('APP','CORREO','INTERNO')),
  datos           jsonb NOT NULL,
  estado          text NOT NULL DEFAULT 'PENDIENTE' CHECK (estado IN ('PENDIENTE','ENVIADA','FALLIDA')),
  clave           text NOT NULL,
  creada_en       timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (clave, canal)
);

-- ---------------------------------------------------------------- trazabilidad (M7)
CREATE TABLE trazabilidad.evento_bitacora (
  evento_id      uuid PRIMARY KEY,                    -- = comun.outbox.evento_id (deduplicación)
  tipo           text NOT NULL,                       -- incluye REPARACION_APLICADA
  correlacion_id uuid,
  pedido_id      uuid,
  tienda_id      smallint,
  sku            text,
  actor          text NOT NULL,
  hora_servidor  timestamptz NOT NULL,
  datos          jsonb NOT NULL
);
CREATE INDEX bitacora_por_pedido ON trazabilidad.evento_bitacora (pedido_id, hora_servidor);
CREATE INDEX bitacora_por_correlacion ON trazabilidad.evento_bitacora (correlacion_id);
CREATE TRIGGER bitacora_inmutable BEFORE UPDATE OR DELETE ON trazabilidad.evento_bitacora
  FOR EACH ROW EXECUTE FUNCTION comun.impedir_modificacion();
