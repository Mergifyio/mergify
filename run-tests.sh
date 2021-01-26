#!/bin/bash

set -exo pipefail

waitport() {
    while ! nc -v -w 1 -i 1 localhost $1 ; do sleep 1 ; done
}

if [ "$CI" != "true" ]; then
    docker-compose up -d --force-recreate --always-recreate-deps --remove-orphans

    waitport 6363

    cleanup () {
        ret=$?
        set +exo pipefail
        docker-compose down --remove-orphans --volumes
        exit $ret
    }
    trap cleanup EXIT
fi

cmd=$1
shift
$cmd "$@"
