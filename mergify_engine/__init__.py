import ddtrace.pin
import redis.asyncio


ddtrace.pin.Pin(service=None).onto(redis.asyncio.Redis)
