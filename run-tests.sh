#!/bin/bash

cleanup(){
    [ -n "$worker0_pid" ] && kill $worker0_pid || true
    [ -n "$worker1_pid" ] && kill $worker1_pid || true
    [ -n "$worker2_pid" ] && kill $worker2_pid || true
    [ -n "$worker3_pid" ] && kill $worker3_pid || true
    [ -n "$worker_beat_pid" ] && kill $worker_beat_pid || true
    [ -n "$bridge_pid" ] && kill -9 $bridge_pid || true
    [ -n "$wsgi_pid" ] && kill -9 $wsgi_pid || true
}
trap "cleanup" exit

set -e

if [ "$MERGIFYENGINE_WEBHOOK_SECRET" == "X" ]; then
    echo "Error: environment variables are not setuped"
    exit 1
fi

celery worker -A mergify_engine.worker -n worker-sub-000@localhost &
worker0_pid="$!"

celery worker -A mergify_engine.worker -n worker-sub-001@localhost &
worker1_pid="$!"

celery worker -A mergify_engine.worker -n worker-free-000@localhost &
worker2_pid="$!"

# For scheduled task
celery worker -A mergify_engine.worker -B &
worker_beat_pid="$!"

uwsgi --http 127.0.0.1:8802 --plugin python3 --master --enable-threads --die-on-term --processes 4 --threads 4 --lazy-apps --thunder-lock --wsgi-file mergify_engine/wsgi.py &
wsgi_pid="$!"

python -u mergify_engine/tests/bridge.py "$@" &
bridge_pid="$!"

wait
