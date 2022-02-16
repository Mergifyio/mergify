#!/bin/bash

set -exo pipefail

if [ "$CI" != "true" ]; then
    docker-compose up -d --force-recreate --always-recreate-deps --remove-orphans

    cleanup () {
        ret="$?"
        set +exo pipefail
        docker-compose down --remove-orphans --volumes
        exit "$ret"
    }
    trap cleanup EXIT
fi

while ! docker run -t --net host --rm redis redis-cli -h localhost -p 6363 keys '*' ; do sleep 1 ; done

cmd="$1"
shift
"$cmd" "$@"
