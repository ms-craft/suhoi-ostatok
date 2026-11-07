#!/bin/sh
set -eu

password=$(cat /run/secrets/redis_password)
case "$password" in
  *[!a-f0-9]*) printf '%s\n' 'Redis password must be lower-case hexadecimal' >&2; exit 1 ;;
esac
if [ "${#password}" -ne 64 ]; then
  printf '%s\n' 'Redis password must contain 64 hexadecimal characters' >&2
  exit 1
fi

umask 077
config=$(mktemp /tmp/pilot-redis.XXXXXX)
printf 'bind 0.0.0.0\nprotected-mode yes\nrequirepass %s\ndir /data\nappendonly yes\nmaxmemory 128mb\nmaxmemory-policy noeviction\n' "$password" > "$config"
chown redis:redis "$config"
exec /usr/local/bin/docker-entrypoint.sh redis-server "$config"
