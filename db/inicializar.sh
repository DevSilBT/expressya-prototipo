#!/bin/bash
# Crea las dos bases (monolito y pagos) y carga el DDL en orden. Solo para el entorno local.
set -e
psql -v ON_ERROR_STOP=1 -U postgres -c "CREATE DATABASE expressya" -c "CREATE DATABASE expressya_pagos"
for f in /docker-entrypoint-initdb.d/core/*.sql; do psql -v ON_ERROR_STOP=1 -U postgres -d expressya -f "$f"; done
for f in /docker-entrypoint-initdb.d/pagos/*.sql; do psql -v ON_ERROR_STOP=1 -U postgres -d expressya_pagos -f "$f"; done
