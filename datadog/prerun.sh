#!/usr/bin/env bash

# If we are in ps:exec do nothing plz
# see https://github.com/DataDog/heroku-buildpack-datadog/issues/155
if [ -z "$DYNO" -o "$DYNOTYPE" = "run" ]; then
    DISABLE_DATADOG_AGENT="true"
    return
fi

case $DYNOTYPE in
    run)
        DISABLE_DATADOG_AGENT="true"
        return
        ;;
    web)
        cat > "$DATADOG_CONF" <<EOF
process_config:
  enabled: "true"
confd_path: $DD_CONF_DIR/conf.d
logs_enabled: true
logs_config:
  frame_size: 30000
additional_checksd: $DD_CONF_DIR/checks.d
tags:
  - dyno:$DYNO
  - dynotype:$DYNOTYPE
  - buildpackversion:$BUILDPACKVERSION
  - appname:$HEROKU_APP_NAME
  - service:web
EOF
        cat > "$DD_CONF_DIR/conf.d/process.d/conf.yaml" <<EOF
init_config:

instances:
  - name: gunicorn-worker
    search_string: ['^gunicorn: worker']
    exact_match: false
    tags:
      - service:web
  - name: gunicorn-master
    search_string: ['^gunicorn: master']
    exact_match: false
    tags:
      - service:web
EOF
        ;;
    worker)
        cat > "$DATADOG_CONF" <<EOF
process_config:
  enabled: "true"
confd_path: $DD_CONF_DIR/conf.d
logs_enabled: true
logs_config:
  frame_size: 30000
additional_checksd: $DD_CONF_DIR/checks.d
tags:
  - dyno:$DYNO
  - dynotype:$DYNOTYPE
  - buildpackversion:$BUILDPACKVERSION
  - appname:$HEROKU_APP_NAME
  - service:worker
EOF
        cat > "$DD_CONF_DIR/conf.d/process.d/conf.yaml" <<EOF
init_config:

instances:
  - name: mergify-engine-worker
    search_string: ['bin/mergify-engine-worker']
    exact_match: false
    tags:
      - service:worker
EOF
        ;;
esac

REDIS_REGEX='^redis(s?)://([^:]*):([^@]+)@([^:]+):([^/?]+)\?db=([^&]*)'

if [ -n "$MERGIFYENGINE_STORAGE_URL" ]; then
    if [[ $MERGIFYENGINE_STORAGE_URL =~ $REDIS_REGEX ]]; then
        [ "${BASH_REMATCH[1]}" ] && REDIS_SSL="true" || REDIS_SSL="false"
        sed -i "s/<CACHE SSL>/$REDIS_SSL/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<CACHE HOST>/${BASH_REMATCH[4]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<CACHE PASSWORD>/${BASH_REMATCH[3]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<CACHE PORT>/${BASH_REMATCH[5]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<CACHE DB>/${BASH_REMATCH[6]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
    fi
fi

if [ -n "$MERGIFYENGINE_STREAM_URL" ]; then
    if [[ $MERGIFYENGINE_STREAM_URL =~ $REDIS_REGEX ]]; then
        [ "${BASH_REMATCH[1]}" ] && REDIS_SSL="true" || REDIS_SSL="false"
        sed -i "s/<STREAM SSL>/$REDIS_SSL/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<STREAM HOST>/${BASH_REMATCH[4]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<STREAM PASSWORD>/${BASH_REMATCH[3]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<STREAM PORT>/${BASH_REMATCH[5]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        sed -i "s/<STREAM DB>/${BASH_REMATCH[6]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
    fi
fi

# Workaround for https://github.com/DataDog/heroku-buildpack-datadog/issues/155
# When datadog.sh is called it will copy the example and project confd, and
# overwrite our conf
cp -f "$DATADOG_CONF" "$DATADOG_CONF.example"
cp -f "$DD_CONF_DIR/conf.d/process.d/conf.yaml" "$APP_DATADOG_CONF_DIR/process.yaml"
cp -f "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml" "$APP_DATADOG_CONF_DIR/redisdb.yaml"
