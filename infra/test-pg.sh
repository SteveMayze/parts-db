#!/bin/sh
# Runs the whole test suite against a THROW-AWAY PostgreSQL container on this machine (make test-pg).
# The container is started with --rm on a free loopback port, with a random password that exists only in this run, and it
# is removed again when the run ends (also on Ctrl-C or failure). Nothing is installed or kept; nothing touches a server.
# The PostgreSQL client tools (pg_dump, pg_restore) must be on the PATH or in DJANGO_PG_BIN_DIR, or the backup tests are skipped.
set -eu
IMAGE="${PG_TEST_IMAGE:-docker.io/library/postgres:17}"
PYTHON="${PYTHON:-.venv/bin/python}"
NAME="partsdb-test-pg-$$"
PASSWORD="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
PORT="$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"

cleanup() { podman rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

echo "Starting a throw-away PostgreSQL ($IMAGE) on 127.0.0.1:$PORT ..."
podman run -d --rm --name "$NAME" -p "127.0.0.1:$PORT:5432" \
  -e POSTGRES_USER=partsdb -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB=partsdb "$IMAGE" >/dev/null

i=0
until podman exec "$NAME" pg_isready -q -U partsdb -d partsdb 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -le 60 ] || { echo "PostgreSQL did not become ready in time." >&2; exit 1; }
  sleep 1
done
# The server restarts once during the image's own initialisation; wait until it answers a real query.
until podman exec "$NAME" psql -q -U partsdb -d partsdb -c 'select 1' >/dev/null 2>&1; do sleep 1; done

DJANGO_DB_HOST=127.0.0.1 DJANGO_DB_PORT="$PORT" DJANGO_DB_NAME=partsdb DJANGO_DB_USER=partsdb DJANGO_DB_PASSWORD="$PASSWORD" \
  "$PYTHON" manage.py test "$@"
