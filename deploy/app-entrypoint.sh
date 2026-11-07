#!/bin/sh
set -eu

read_secret() { cat "/run/secrets/$1"; }
require_hex() {
  case "$2" in
    *[!a-f0-9]*) printf '%s must contain 64 lower-case hexadecimal characters\n' "$1" >&2; exit 1 ;;
  esac
  if [ "${#2}" -ne 64 ]; then
    printf '%s must contain 64 lower-case hexadecimal characters\n' "$1" >&2
    exit 1
  fi
}

db_password=$(read_secret database_password)
require_hex database_password "$db_password"
export DATABASE_URL="postgresql+psycopg://${APP_DATABASE_USER:-stopkadr_runtime}:$db_password@postgres:5432/stopkadr"
if [ "$1" = "alembic" ]; then
  exec "$@"
fi
redis_password=$(read_secret redis_password)
require_hex redis_password "$redis_password"
export REDIS_URL="redis://:$redis_password@redis:6379/0"
export S3_ACCESS_KEY="$(read_secret s3_access_key)"
export S3_SECRET_KEY="$(read_secret s3_secret_key)"
export FAKE_PAYMENT_SECRET="$(read_secret fake_payment_secret)"
export PILOT_ACCESS_CODE="$(read_secret pilot_access_code)"
export PILOT_ALLOWED_EMAILS="$(read_secret pilot_allowed_emails)"
if [ -s /run/secrets/gemini_api_key ]; then
  export GEMINI_API_KEY="$(read_secret gemini_api_key)"
fi

case "${S3_ENDPOINT_URL:-}" in
  https://*) ;;
  *) printf '%s\n' 'Configure an HTTPS S3_ENDPOINT_URL for the pilot' >&2; exit 1 ;;
esac

exec "$@"
