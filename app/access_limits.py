"""Pilot auth backstop shared by API replicas through Redis."""

from hashlib import sha256
from hmac import digest

from fastapi import Request
from redis.exceptions import RedisError

from app.core import ApiError

AUTH_WINDOW_SECONDS = 60
AUTH_ATTEMPTS = 8
_INCREMENT = """
local attempts = redis.call("INCR", KEYS[1])
if attempts == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end
return attempts
"""


def enforce_auth_limit(request: Request, email: str, purpose: str) -> None:
    state = request.app.state
    seed = state.settings.pilot_access_code.get_secret_value().encode()
    identity = digest(seed, f"{purpose}:{email}".encode(), sha256).hex()
    key = f"{state.settings.redis_queue_key}:auth:{identity}"
    try:
        attempts = state.queue.client.eval(_INCREMENT, 1, key, AUTH_WINDOW_SECONDS)
    except (AttributeError, RedisError) as error:
        raise ApiError(503, "auth_unavailable", "Вход временно недоступен.") from error
    if attempts > AUTH_ATTEMPTS:
        raise ApiError(429, "auth_rate_limited", "Слишком много попыток. Попробуйте через минуту.")
