# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# NOTE(sileht): usefull for gunicon, not really for uwsgi
# import gevent
# import gevent.monkey
# gevent.monkey.patch_all()

import json
import logging
import os

try:
    from hmac import compare_digest
except ImportError:
    # NOTE(sileht): For python <= 2.7.6, like travis as on trusty...
    from operator import _compare_digest as compare_digest


import flask
import github
import rq
# import rq_dashboard

from mergify_engine import config
from mergify_engine import utils
from mergify_engine import worker


LOG = logging.getLogger(__name__)

app = flask.Flask(__name__)

# app.config.from_object(rq_dashboard.default_settings)
# app.register_blueprint(rq_dashboard.blueprint, url_prefix="/rq")
# app.config["REDIS_URL"] = utils.get_redis_url()
# app.config["RQ_POLL_INTERVAL"] = 10000  # ms


def get_redis():
    if not hasattr(flask.g, 'redis'):
        conn = utils.get_redis()
        flask.g.redis = conn
    return flask.g.redis


def get_queue():
    if not hasattr(flask.g, 'rq_queue'):
        flask.g.rq_queue = rq.Queue(connection=get_redis())
    return flask.g.rq_queue


@app.route("/refresh/<owner>/<repo>/<path:refresh_ref>",
           methods=["POST"])
def refresh(owner, repo, refresh_ref):
    authentification()

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    installation_id = utils.get_installation_id(integration, owner)
    if not installation_id:
        flask.abort(400, "%s have not installed mergify_engine" % owner)

    token = integration.get_access_token(installation_id).token
    g = github.Github(token)
    r = g.get_repo("%s/%s" % (owner, repo))
    if refresh_ref == "full" or refresh_ref.startswith("branch/"):
        if refresh_ref.startswith("branch/"):
            branch = refresh_ref[7:]
            pulls = r.get_pulls(base=branch)
        else:
            branch = '*'
            pulls = r.get_pulls()
        key = "queues~%s~%s~%s~%s" % (installation_id, owner, repo, branch)
        utils.get_redis().delete(key)
    else:
        pulls = [r.get_pull(int(refresh_ref[5:]))]
    for p in pulls:
        # Mimic the github event format
        data = {
            'repository': r.raw_data,
            'installation': {'id': installation_id},
            'pull_request': p.raw_data,
        }
        get_queue().enqueue(worker.event_handler, "refresh", data)

    return "", 202


@app.route("/refresh", methods=["POST"])
def refresh_all():
    authentification()

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    counts = [0, 0, 0]
    for install in utils.get_installations(integration):
        counts[0] += 1
        token = integration.get_access_token(install["id"]).token
        g = github.Github(token)
        i = g.get_installation(install["id"])

        for repo in i.get_repos():
            counts[1] += 1
            pulls = repo.get_pulls()
            branches = set([p.base.ref for p in pulls])

            # Mimic the github event format
            for branch in branches:
                counts[2] += 1
                get_queue().enqueue(worker.event_handler, "refresh", {
                    'repository': repo.raw_data,
                    'installation': {'id': install['id']},
                    'refresh_ref': "branch/%s" % branch,
                })
    return ("Updated %s installations, %s repositories, "
            "%s branches" % tuple(counts)), 202


@app.route("/queue/<owner>/<repo>/<path:branch>")
def queue(owner, repo, branch):
    return get_redis().get("queues~%s~%s~%s" % (owner, repo, branch)) or "[]"


def _get_status(r, installation_id):
    queues = []
    for key in r.keys("queues~%s~*~*" % installation_id):
        _, _, owner, repo, branch = key.decode("utf8").split("~")
        payload = r.hgetall(key)
        pulls = [json.loads(p.decode("utf8")) for p in payload.values()]
        if pulls:
            updated_at = list(sorted([p["updated_at"] for p in pulls]))[-1]
        else:
            updated_at = None
        queues.append({
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "pulls": pulls,
            "updated_at": updated_at,
        })
    return json.dumps(queues)


@app.route("/status/<installation_id>")
def status(installation_id):
    r = get_redis()
    return _get_status(r, installation_id)


def stream_message(_type, data):
    return 'event: %s\ndata: %s\n\n' % (_type, data)


def stream_generate(installation_id):
    r = get_redis()
    yield stream_message("refresh", _get_status(r, installation_id))
    pubsub = r.pubsub()
    pubsub.subscribe("update-%s", installation_id)
    while True:
        # NOTE(sileht): heroku timeout is 55s, we have set gunicorn timeout
        # to 60s, this assume 5s is enough for http and redis round strip and
        # use 50s
        message = pubsub.get_message(timeout=50.0)
        if message is None:
            yield stream_message("ping", "{}")
        elif message["channel"] == "update-%s" % installation_id:
            yield stream_message("refresh", _get_status(r, installation_id))


@app.route('/status/stream/<installation_id>')
def stream(installation_id):
    return flask.Response(flask.stream_with_context(
        stream_generate(installation_id)
    ), mimetype="text/event-stream")


@app.route("/event", methods=["POST"])
def event_handler():
    authentification()

    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()

    if event_type in ["refresh", "pull_request", "status",
                      "pull_request_review"]:
        get_queue().enqueue(worker.event_handler, event_type, data)

    if "repository" in data:
        repo_name = data["repository"]["full_name"]
    else:
        repo_name = data["installation"]["account"]["login"]

    LOG.info('[%s/%s] received "%s" event "%s"',
             data["installation"]["id"], repo_name,
             event_type, event_id)

    return "", 202


@app.route("/")
def index():
    return flask.redirect("https://mergify.io/")


@app.route("/<installation_id>")
def installation(installation_id):
    return app.send_static_file("index.html")


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.route("/fonts/<file>")
def fonts(file):
    # bootstrap fonts
    return flask.send_from_directory(os.path.join("static", "fonts"), file)


def authentification():
    # Only SHA1 is supported
    header_signature = flask.request.headers.get('X-Hub-Signature')
    if header_signature is None:
        LOG.warning("Webhook without signature")
        flask.abort(403)

    try:
        sha_name, signature = header_signature.split('=')
    except ValueError:
        sha_name = None

    if sha_name != 'sha1':
        LOG.warning("Webhook signature malformed")
        flask.abort(403)

    mac = utils.compute_hmac(flask.request.data)
    if not compare_digest(mac, str(signature)):
        LOG.warning("Webhook signature invalid")
        flask.abort(403)
