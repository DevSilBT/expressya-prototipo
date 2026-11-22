#!/usr/bin/env bash
# Uso: PGHOST=... PGPORT=... PGUSER=... ./tests/sql/correr_pruebas_sql.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
DB=expressya_pruebas
dropdb --if-exists "$DB"; createdb "$DB"
for f in db/core/0*.sql; do
  psql -q -v ON_ERROR_STOP=1 -d "$DB" -f "$f"
done
echo "== TP-S3-04 · última unidad: 100 clientes concurrentes (pgbench)"
pgbench -n -c 100 -j 10 -t 1 -f tests/sql/ultima_unidad.pgbench.sql "$DB" | grep -E "number of (clients|transactions actually processed|failed)"
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f tests/sql/verificar_ultima_unidad.sql
echo "== TP-S3-11 · sin bloqueos mutuos: 50 clientes x 4 reservas con orden de líneas opuesto"
psql -q -v ON_ERROR_STOP=1 -d "$DB" -c "SELECT inventario.cargar_existencia(4::smallint,'SKU-Y',1000), inventario.cargar_existencia(4::smallint,'SKU-Z',1000)"
pgbench -n -c 50 -j 10 -t 4 -f tests/sql/orden_inverso.pgbench.sql "$DB" | grep -E "number of (transactions actually processed|failed)"
psql -q -d "$DB" -c "SELECT count(*) AS reservas_activas_tienda4 FROM inventario.reserva WHERE tienda_id = 4 AND estado = 'ACTIVA'"
echo "== Pruebas funcionales"
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f tests/sql/pruebas_funcionales.sql
echo "== Pruebas de C3 (surtido, faltante, conteo y ajuste)"
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f tests/sql/pruebas_c3.sql
echo "== Reconstrucción de todos los saldos (al final)"
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f tests/sql/reconstruccion.sql
echo "TODAS LAS PRUEBAS SQL PASARON"
