#!/bin/bash
cd /app

get_command() {
    sed -n -e "s/^$1://p" Procfile
}

MODE=${1:-aio}

if [ "$MERGIFYENGINE_INTEGRATION_ID" ]; then
  case ${MODE} in
      web|worker) exec $(get_command $1);;
      aio) exec honcho start;;
      *) echo "usage: $0 (web|worker|aio)";;
  esac
elif [ "$MERGIFYENGINE_INSTALLER" ]; then
  exec honcho -f installer/Procfile start
else
    echo "MERGIFYENGINE_INTEGRATION_ID or MERGIFYENGINE_INSTALLER must set"
fi
exit 1
