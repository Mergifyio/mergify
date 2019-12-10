#!/usr/bin/env bash

# Disable the Datadog Agent based on dyno type
if [ "$DYNOTYPE" == "run" ]; then
  DISABLE_DATADOG_AGENT="true"
fi

# Only the first web dyno will monitor redis
if [ "$DYNO" == "web.1" ]; then
    REDIS_REGEX='^redis://([^:]+):([^@]+)@([^:]+):([^/]+)$'

    if [ -n "$MERGIFYENGINE_STORAGE_URL" ]; then
        if [[ $MERGIFYENGINE_STORAGE_URL =~ $REDIS_REGEX ]]; then
        sed -i "s/<CACHE HOST>/${BASH_REMATCH[3]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<CACHE PASSWORD>/${BASH_REMATCH[2]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<CACHE PORT>/${BASH_REMATCH[4]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        fi
    fi


    if [ -n "$MERGIFYENGINE_CELERY_BROKER_URL" ]; then
        if [[ $MERGIFYENGINE_CELERY_BROKER_URL =~ $REDIS_REGEX ]]; then
            sed -i "s/<CELERY HOST>/${BASH_REMATCH[3]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
            sed -i "s/<CELERY PASSWORD>/${BASH_REMATCH[2]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
            sed -i "s/<CELERY PORT>/${BASH_REMATCH[4]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        fi
    fi

else
    rm -f $DD_CONF_DIR/conf.d/redisdb.d/conf.yaml
fi
