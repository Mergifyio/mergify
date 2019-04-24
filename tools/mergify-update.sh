#!/bin/bash

set -euxo pipefail

# TODO(sileht): nginx cache the ip of the engine container, when the
# engine container is recreated, nginx still try to connect to the old one
# So we temporary restart the nginx container each time we deploy mergify

cd mergify-engine-docker
export ENV=prod

docker-compose -f docker-compose.yaml pull engine

# NOTE(sileht): Upgrade queue set to zset
# TODO(
docker-compose -f docker-compose.yaml stop engine-beat engine
cat > upgrade.lua << EOF
local queues = redis.call("keys", "strict-merge-queues~*")
for idx, queue in ipairs(queues) do
    if redis.call("type", queue) ~= "zset" then
        local oldqueue = "deprecated-set-" .. queue
        redis.call("RENAME", queue, oldqueue)
        redis.call("ZUNIONSTORE", queue, 1, oldqueue)
    end
end
EOF
docker cp upgrade.lua mergify_celery_1:/tmp/
docker exec mergify_celery_1 redis-cli -h storage --eval /tmp/upgrade.lua
docker exec mergify_celery_1 rm -f /tmp/upgrade.lua
rm -f upgrade.lua

docker-compose -f docker-compose.yaml up -d
docker restart mergify_nginx_1

