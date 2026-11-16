-- ExpressYa · Base de datos del servicio de Pagos (M4), separada del monolito (ADR-17)
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS pagos;

CREATE TABLE pagos.pago (
  pago_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pedido_id             uuid NOT NULL,
  clave                 text NOT NULL UNIQUE,            -- K2: una sola llamada de cobro por clave (ADR-05)
  monto                 numeric(12,2) NOT NULL CHECK (monto > 0),
  estado                text NOT NULL CHECK (estado IN ('SOLICITADO','APROBADO','RECHAZADO','INCIERTO','EN_REVISION_FINANZAS')),
  referencia_pasarela   text,
  aprobado_en_pasarela  timestamptz,                     -- informativo; no decide la reserva (R-D02)
  recibido_en           timestamptz,
  solicitado_en         timestamptz NOT NULL DEFAULT clock_timestamp(),
  alerta_enviada_en     timestamptz,
  resuelto_por          text,
  evidencia_resolucion  text,
  CONSTRAINT resolucion_manual CHECK (resuelto_por IS NULL OR evidencia_resolucion IS NOT NULL)
);
-- Un pedido no puede tener dos cobros vivos a la vez (evita doble cargo tras un timeout)
CREATE UNIQUE INDEX pago_un_vivo_por_pedido ON pagos.pago (pedido_id)
  WHERE estado IN ('SOLICITADO','APROBADO','INCIERTO','EN_REVISION_FINANZAS');
CREATE INDEX pago_inciertos ON pagos.pago (solicitado_en) WHERE estado = 'INCIERTO';

CREATE TABLE pagos.notificacion_pago (
  notificacion_id  text PRIMARY KEY,                     -- N1 (deduplicación)
  pago_id          uuid NOT NULL REFERENCES pagos.pago,
  resultado        text NOT NULL CHECK (resultado IN ('APROBADO','RECHAZADO')),
  aprobado_en      timestamptz,
  contenido        jsonb NOT NULL,
  recibido_en      timestamptz NOT NULL DEFAULT clock_timestamp(),
  conflicto        boolean NOT NULL DEFAULT false        -- resultado contradictorio: se registra, no cambia el estado
);

CREATE TABLE pagos.ajuste_cobro (
  ajuste_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pago_id            uuid NOT NULL REFERENCES pagos.pago,
  pedido_id          uuid NOT NULL,
  tipo               text NOT NULL CHECK (tipo IN ('PARCIAL','TOTAL','ESCALADO')),
  componente         text NOT NULL CHECK (componente IN ('MERCANCIA','ENVIO','PROPINA','PROMOCION')),
  monto              numeric(12,2) NOT NULL CHECK (monto > 0),
  motivo             text NOT NULL,
  estado             text NOT NULL CHECK (estado IN ('SOLICITADO','APLICADO','FALLIDO','INCIERTO','EN_REVISION_FINANZAS','PENDIENTE_MANUAL')),
  clave              text NOT NULL UNIQUE,               -- K4
  referencia_externa text,
  usuario_manual     text,
  creado_en          timestamptz NOT NULL DEFAULT clock_timestamp(),
  aplicado_en        timestamptz,
  CONSTRAINT automatico_solo_mercancia CHECK (componente = 'MERCANCIA' OR tipo = 'ESCALADO'),   -- PD05
  CONSTRAINT aplicado_con_referencia CHECK (estado <> 'APLICADO' OR referencia_externa IS NOT NULL)
);

CREATE TABLE pagos.operacion_procesada (
  clave      text PRIMARY KEY,
  tipo       text NOT NULL,
  huella     text NOT NULL,
  resultado  jsonb,
  creada_en  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE pagos.outbox (
  evento_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tipo          text NOT NULL,           -- PagoAprobado, PagoRechazado, PagoIncierto, PagoEnRevisionFinanzas, AjusteCobro*
  agregado_id   text NOT NULL,
  datos         jsonb NOT NULL,
  creado_en     timestamptz NOT NULL DEFAULT clock_timestamp(),
  entregado_en  timestamptz,
  intentos      int NOT NULL DEFAULT 0
);
CREATE INDEX pagos_outbox_pendientes ON pagos.outbox (creado_en) WHERE entregado_en IS NULL;
