#!/bin/sh
# Sobe um PostgreSQL local (sem Docker) na porta 54329, usuário e banco "aeroledger".
# Uso:   scripts/pg_local.sh [pasta-dos-dados]     (padrão: ./.pgdata)
# Parar: pg_ctl -D <pasta-dos-dados> stop
set -e
PGBIN=${PGBIN:-$(pg_config --bindir 2>/dev/null || echo /usr/lib/postgresql/16/bin)}
DADOS=${1:-$(pwd)/.pgdata}
if [ ! -d "$DADOS" ]; then
  "$PGBIN/initdb" -D "$DADOS" -U aeroledger --auth=trust -E UTF8 >/dev/null
fi
"$PGBIN/pg_ctl" -D "$DADOS" -o "-p 54329 -k /tmp" -l "$DADOS/log" start
"$PGBIN/psql" -h localhost -p 54329 -U aeroledger -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'aeroledger'" | grep -q 1 \
  || "$PGBIN/createdb" -h localhost -p 54329 -U aeroledger aeroledger
echo "PostgreSQL em postgresql://aeroledger@localhost:54329/aeroledger"
