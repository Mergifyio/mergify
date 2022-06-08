#!/bin/bash

set -exo pipefail

if [ "$CI" == "true" ]; then
    REDIS_PORT=6363
else
    docker compose up -d --force-recreate --always-recreate-deps --remove-orphans

    cleanup () {
        ret="$?"
        set +exo pipefail
        docker-compose down --remove-orphans --volumes
        exit "$ret"
    }
    trap cleanup EXIT

    REDIS_PORT=$(docker compose ps redis --format=json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['Publishers'][0]['PublishedPort'])")
fi

while ! docker run -t --net host --rm redis redis-cli -h localhost -p "$REDIS_PORT" keys '*' ; do sleep 1 ; done

export MERGIFYENGINE_DEFAULT_REDIS_URL="redis://localhost:${REDIS_PORT}"

cmd="$1"
shift
"$cmd" "$@"
