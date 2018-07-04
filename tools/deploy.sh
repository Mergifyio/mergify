#!/bin/bash
set -e

error() {
    echo "$1 unset, exiting"
    exit 1
}
[ ! $PRODUCTION_PORT ] && error PRODUCTION_PORT
[ ! $PRODUCTION_URL ] && error PRODUCTION_URL
[ ! $PRODUCTION_KEY ] && error PRODUCTION_KEY
[ ! $PRODUCTION_KNOWN_HOSTS ] && error PRODUCTION_KNOWN_HOSTS
[ ! $SENTRY_SLUG ] && error SENTRY_SLUG
[ ! $SENTRY_TOKEN ] && error SENTRY_TOKEN

commit_id="$(git rev-parse HEAD)"
previous_commit_id="$(git rev-parse HEAD^)"
commit_subject=$(git log -1 --pretty="%s")

# ssh port looks not working with git version of travis
mkdir -p ~/.ssh
cat > ~/.ssh/config <<EOF
Host *
  Port $PRODUCTION_PORT
EOF

echo "$PRODUCTION_KEY" | base64 -d > ~/.ssh/id_ed25519
echo "$PRODUCTION_KNOWN_HOSTS" | base64 -d > ~/.ssh/known_hosts
chmod 600 ~/.ssh/id_ed25519

# Create a new release
curl https://sentry.io/api/0/organizations/${SENTRY_SLUG}/releases/ \
  -X POST \
  -H "Authorization: Bearer ${SENTRY_SLUG}" \
  -H "Content-Type: application/json" \
  -d '
  {
    "version": "2da95dfb052f477380608d59d32b4ab9",
    "refs": [{
        "repository":"mergifyio/merg",
        "commit":"'${commit_id}'",
        "previousCommit":"'${previous_commit_id}'"
    }],
    "projects": ["mergify-engine"]
}
'

# Create a new deploy
git remote add production $PRODUCTION_URL
git push production --force HEAD:master
curl https://sentry.io/api/0/organizations/$SENTRY_SLUG/releases/${commit_id}/deploys/ \
  -X POST \
  -H "Authorization: Bearer ${SENTRY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '
  {
    "environment": "production",
    "name": "'${commit_subject}'",
    "url": "https://github.com/Mergifyio/mergify-engine/commit/'${commit_id}'"
}
'
