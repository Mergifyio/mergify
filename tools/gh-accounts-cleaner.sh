#!/bin/bash

for token in $MERGIFYENGINE_MAIN_TOKEN $MERGIFYENGINE_FORK_TOKEN ; do
    for i in $(curl -qq -H "Authorization: token $token" https://api.github.com/user/repos 2>/dev/null | jq -r '.[].full_name'); do
        curl -qq -H "Authorization: token $token" -X DELETE https://api.github.com/repos/$i
    done
done

