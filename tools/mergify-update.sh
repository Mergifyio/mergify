#!/bin/bash

set -euxo pipefail

# TODO(sileht): nginx cache the ip of the engine container, when the
# engine container is recreated, nginx still try to connect to the old one
# So we temporary restart the nginx container each time we deploy mergify

cd mergify-engine-docker
export ENV=prod

docker-compose -f docker-compose.yaml pull engine
docker-compose -f docker-compose.yaml up -d
docker restart mergify_nginx_1

