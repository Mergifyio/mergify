#!/bin/bash

cleanup(){
    [ -n "$worker0_pid" ] && kill -9 $worker0_pid || true
    [ -n "$worker1_pid" ] && kill -9 $worker1_pid || true
    [ -n "$bridge_pid" ] && kill -9 $bridge_pid || true
    [ -n "$wsgi_pid" ] && kill -9 $wsgi_pid || true
}
trap "cleanup" exit

set -e

if [ "$MERGIFYENGINE_WEBHOOK_SECRET" == "X" ]; then
    echo "Error: environment variables are not setuped"
    exit 1
fi

mergify-rq-worker --fqdn localhost 0 &
worker0_pid="$!"

mergify-rq-worker --fqdn localhost 1 &
worker1_pid="$!"

uwsgi --http 127.0.0.1:8802 --plugin python3 --master --enable-threads --die-on-term --processes 4 --threads 4 --lazy-apps --thunder-lock --wsgi-file mergify_engine/wsgi.py &
wsgi_pid="$!"

python -u mergify_engine/tests/bridge.py "$@" &
bridge_pid="$!"

wait
