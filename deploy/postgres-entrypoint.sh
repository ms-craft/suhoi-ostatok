#!/bin/sh
set -eu
export POSTGRES_PASSWORD="$(cat /run/secrets/postgres_admin_password)"
export POSTGRES_MIGRATION_PASSWORD="$(cat /run/secrets/postgres_migration_password)"
export POSTGRES_RUNTIME_PASSWORD="$(cat /run/secrets/postgres_runtime_password)"
for password in "$POSTGRES_PASSWORD" "$POSTGRES_MIGRATION_PASSWORD" "$POSTGRES_RUNTIME_PASSWORD"; do
  case "$password" in
    *[!a-f0-9]*) printf '%s\n' 'PostgreSQL passwords must be lower-case hexadecimal' >&2; exit 1 ;;
  esac
  if [ "${#password}" -ne 64 ]; then
    printf '%s\n' 'PostgreSQL passwords must contain 64 hexadecimal characters' >&2
    exit 1
  fi
done
exec /usr/local/bin/docker-entrypoint.sh postgres -c log_parameter_max_length=0 -c log_parameter_max_length_on_error=0 -c log_statement=none -c log_min_error_statement=panic
