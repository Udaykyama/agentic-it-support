import hashlib

from app.errors import APIError

_INCREMENT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], 60) end
return {count, redis.call('TTL', KEYS[1])}
"""


def enforce_limit(redis, scope, identity, limit):
    key = hashlib.sha256(f"{scope}:{identity}".encode("utf-8")).hexdigest()
    count, ttl = redis.eval(_INCREMENT, 1, f"neuraldesk:limit:{key}")
    if count > limit:
        raise APIError(
            429, "rate_limit_exceeded", "Too many requests. Wait before trying again.",
            {"retry_after": max(1, ttl)},
        )
