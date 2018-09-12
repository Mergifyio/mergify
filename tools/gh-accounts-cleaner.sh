#!/bin/bash

MERGIFYENGINE_MAIN_TOKEN=$(python3 -c 'import yaml ; print(yaml.load(open("test.yml").read())["MAIN_TOKEN_DELETE"])')
MERGIFYENGINE_FORK_TOKEN=$(python3 -c 'import yaml ; print(yaml.load(open("test.yml").read())["FORK_TOKEN_DELETE"])')
for token in $MERGIFYENGINE_MAIN_TOKEN $MERGIFYENGINE_FORK_TOKEN ; do
    for i in $(curl -qq -H "Authorization: token $token" https://api.github.com/user/repos 2>/dev/null | jq -r '.[].full_name'); do
        echo $i
        curl -qq -H "Authorization: token $token" -X DELETE https://api.github.com/repos/$i
    done
done

