web: gunicorn -k uvicorn.workers.UvicornH11Worker --log-level warning mergify_engine.web.asgi
worker: mergify-engine-worker
bridge: python -u mergify_engine/tests/bridge.py --clean
