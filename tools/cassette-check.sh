#!/bin/bash

for name in OAUTH_CLIENT_ID OAUTH_CLIENT_SECRET WEBHOOK_SECRET FORK_TOKEN MAIN_TOKEN; do
    value=$(python3 -c "import yaml; print(yaml.load(open('test.yml'))['${name}'])")
    echo "$name=$value"
    grep -r "$value" mergify_engine/tests/fixtures/cassettes/ && exit 1
done
