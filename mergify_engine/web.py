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

import hmac
import json
import logging
import os

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


INTEGRATION = github.GithubIntegration(config.INTEGRATION_ID,
                                       config.PRIVATE_KEY)


def get_queue():
    if not hasattr(flask.g, 'rq_queue'):
        flask.g.rq_queue = rq.Queue(connection=utils.get_redis_for_rq())
    return flask.g.rq_queue


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
    if not hmac.compare_digest(mac, str(signature)):
        LOG.warning("Webhook signature invalid")
        flask.abort(403)


@app.route("/check_status_msg/<path:key>")
def check_status_msg(key):
    msg = utils.get_redis_for_cache().hget("status", key)
    if msg:
        return flask.render_template("msg.html", msg=msg)
    else:
        flask.abort(404)


@app.route("/refresh/<owner>/<repo>/<path:refresh_ref>",
           methods=["POST"])
def refresh(owner, repo, refresh_ref):
    authentification()

    installation_id = utils.get_installation_id(INTEGRATION, owner)
    if not installation_id:
        flask.abort(400, "%s have not installed mergify_engine" % owner)

    token = INTEGRATION.get_access_token(installation_id).token
    g = github.Github(token)
    r = g.get_repo("%s/%s" % (owner, repo))
    if refresh_ref == "full" or refresh_ref.startswith("branch/"):
        if refresh_ref.startswith("branch/"):
            branch = refresh_ref[7:]
            pulls = r.get_pulls(base=branch)
        else:
            branch = '*'
            pulls = r.get_pulls()
        key = "queues~%s~%s~%s~%s~%s" % (installation_id, owner, repo,
                                         r.private, branch)
        utils.get_redis_for_cache().delete(key)
    else:
        try:
            pull_number = int(refresh_ref[5:])
        except ValueError:
            return "Invalid PR ref", 400
        pulls = [r.get_pull(pull_number)]

    subscription = utils.get_subscription(utils.get_redis_for_cache(),
                                          installation_id)
    if not subscription["token"]:
        return "", 202

    if r.private and not subscription["subscribed"]:
        return "", 202

    for p in pulls:
        # Mimic the github event format
        data = {
            'repository': r.raw_data,
            'installation': {'id': installation_id},
            'pull_request': p.raw_data,
        }
        get_queue().enqueue(worker.event_handler, "refresh",
                            subscription, data)

    return "", 202


@app.route("/refresh", methods=["POST"])
def refresh_all():
    authentification()

    counts = [0, 0, 0]
    for install in utils.get_installations(INTEGRATION):
        counts[0] += 1
        token = INTEGRATION.get_access_token(install["id"]).token
        g = github.Github(token)
        i = g.get_installation(install["id"])

        subscription = utils.get_subscription(utils.get_redis_for_cache(),
                                              install["id"])
        if not subscription["token"]:
            continue

        for r in i.get_repos():
            if r.private and not subscription["subscribed"]:
                continue

            counts[1] += 1
            pulls = r.get_pulls()
            for p in pulls:
                # Mimic the github event format
                data = {
                    'repository': r.raw_data,
                    'installation': {'id': install["id"]},
                    'pull_request': p.raw_data,
                }
                get_queue().enqueue(worker.event_handler, "refresh", data)

    return ("Updated %s installations, %s repositories, "
            "%s branches" % tuple(counts)), 202


@app.route("/queue/<owner>/<repo>/<path:branch>")
def queue(owner, repo, branch):
    r = utils.get_redis_for_cache()
    installation_id = utils.get_installation_id(INTEGRATION, owner)
    return r.get("queues~%s~%s~%s~%s~%s" % (installation_id, owner, repo,
                                            False, branch)) or "[]"


def _get_status(r, installation_id, login='*', repo='*'):
    queues = []
    for key in r.keys("queues~%s~%s~%s~%s~*" % (installation_id, login, repo,
                                                False)):
        _, _, owner, repo, private, branch = key.split("~")
        payload = r.hgetall(key)
        pulls = [json.loads(p) for p in payload.values()]
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


def stream_message(_type, data):
    return 'event: %s\ndata: %s\n\n' % (_type, data)


def stream_generate(installation_id, login="*", repo="*"):
    r = utils.get_redis_for_cache()
    yield stream_message("refresh", _get_status(r, installation_id,
                                                login, repo))
    pubsub = r.pubsub()
    pubsub.subscribe("update-%s" % installation_id)
    while True:
        # NOTE(sileht): heroku timeout is 55s, we have set gunicorn timeout
        # to 60s, this assume 5s is enough for http and redis round strip and
        # use 50s
        message = pubsub.get_message(timeout=50.0)
        if message is None:
            yield stream_message("ping", "{}")
        elif message["channel"] == "update-%s" % installation_id:
            yield stream_message("refresh", _get_status(r, installation_id,
                                                        login, repo))


@app.route("/status/install/<installation_id>/")
def status(installation_id):
    r = utils.get_redis_for_cache()
    return _get_status(r, installation_id)


@app.route("/status/repos/<login>/")
@app.route("/status/repos/<login>/<repo>/")
def status_repo(login, repo="*"):
    r = utils.get_redis_for_cache()
    installation_id = utils.get_installation_id(INTEGRATION, login)
    return _get_status(r, installation_id, login, repo)


@app.route('/stream/status/repos/<login>/')
@app.route('/stream/status/repos/<login>/<repo>/')
def stream_repo(login, repo="*"):
    installation_id = utils.get_installation_id(INTEGRATION, login)
    return flask.Response(flask.stream_with_context(
        stream_generate(installation_id, login, repo)
    ), mimetype="text/event-stream")


@app.route('/stream/status/install/<installation_id>/')
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

    if event_type == "installation" and data["action"] == "deleted":
        key = "queues~%s~*~*~*~*" % data["installation"]["id"]
        utils.get_redis_for_cache().delete(key)
        msg_action = "handled, cache cleaned"

    elif event_type == "installation":
        msg_action = "ignored (action %s)" % data["action"]

    elif event_type in ["pull_request", "pull_request_review", "status",
                        "refresh"]:

        subscription = utils.get_subscription(
            utils.get_redis_for_cache(), data["installation"]["id"])

        if not subscription["token"]:
            msg_action = "ignored (no token)"

        elif data["repository"]["private"] and not subscription["subscribed"]:
            msg_action = "ignored (not public or subscribe)"

        elif event_type == "status" and data["state"] == "pending":
            msg_action = "ignored (state pending)"

        elif (event_type == "pull_request" and data["action"] not in [
                "opened", "reopened", "closed", "synchronize",
                "labeled", "unlabeled"]):
            msg_action = "ignored (action %s)" % data["action"]

        else:
            get_queue().enqueue(worker.event_handler, event_type,
                                subscription, data)
            msg_action = "pushed to backend"

    else:
        msg_action = "ignored (unexpected event_type)"

    if "repository" in data:
        repo_name = data["repository"]["full_name"]
    else:
        repo_name = data["installation"]["account"]["login"]

    LOG.info('[%s/%s] received "%s" event "%s", %s',
             data["installation"]["id"], repo_name,
             event_type, event_id, msg_action)

    return "", 202


# NOTE(sileht): These endpoints are used for recording cassetes, we receive
# Github event on POST, we store them is redis, GET to retreive and delete
@app.route("/events-testing", methods=["POST", "GET", "DELETE"])
def event_testing_handler():
    r = utils.get_redis_for_cache()
    if flask.request.method == "DELETE":
        r.delete("events-testing")
        return "", 202
    elif flask.request.method == "POST":
        authentification()
        event_type = flask.request.headers.get("X-GitHub-Event")
        event_id = flask.request.headers.get("X-GitHub-Delivery")
        data = flask.request.get_json()
        r.rpush("events-testing", json.dumps(
            {"id": event_id, "type": event_type, "payload": data}
        ))
        return "", 202
    else:
        p = r.pipeline()
        p.lrange("events-testing", 0, -1)
        p.delete("events-testing")
        values = p.execute()[0]
        data = [json.loads(i) for i in values]
        return flask.jsonify(data)


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.route("/fonts/<file>")
def fonts(file):
    # bootstrap fonts
    return flask.send_from_directory(os.path.join("static", "fonts"), file)


@app.route("/")
def index():
    return flask.redirect("https://mergify.io/")


# NOTE(sileht): Must be the last one, since it catch any remaning routes
@app.route("/<path:path>")
def installation(path):
    return app.send_static_file("index.html")
