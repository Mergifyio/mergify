#!/bin/bash

set -euxo pipefail

error() {
    echo "$1 unset, exiting"
    exit 1
}
[ -z "$PRODUCTION_PORT" ] && error PRODUCTION_PORT
[ -z "$PRODUCTION_HOST" ] && error PRODUCTION_HOST
[ -z "$PRODUCTION_KEY" ] && error PRODUCTION_KEY
[ -z "$PRODUCTION_KNOWN_HOSTS" ] && error PRODUCTION_KNOWN_HOSTS

# ssh port looks not working with git version of travis
mkdir -p ~/.ssh

echo "$PRODUCTION_KEY" | base64 -d > ~/.ssh/id_ed25519
echo "$PRODUCTION_KNOWN_HOSTS" | base64 -d > ~/.ssh/known_hosts
chmod 600 ~/.ssh/id_ed25519

docker tag mergify-engine-dev mergifyio/engine:latest
docker login -u $DOCKER_USERNAME -p $DOCKER_PASSWORD
docker push mergifyio/engine:latest

cat tools/mergify-update.sh | ssh -p $PRODUCTION_PORT $PRODUCTION_HOST bash
