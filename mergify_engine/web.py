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

import flask

from flask_cors import cross_origin

from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.tasks import github_events

LOG = logging.getLogger(__name__)

app = flask.Flask(__name__, static_url_path='')


def authentification():  # pragma: no cover
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


@app.route("/badges/<owner>/<repo>.png")
def badge(owner, repo):
    redis = utils.get_redis_for_cache()
    mode = ("enabled" if redis.sismember("badges", owner + "/" + repo)
            else "disabled")
    style = flask.request.args.get("style", "cut")
    return flask.send_from_directory("../doc/source/_static",
                                     "badge-%s-%s.png" % (mode, style))


@app.route("/validate", methods=["POST"])
@cross_origin()
def config_validator():
    try:
        rules.validate_user_config(flask.request.files['data'].stream)
    except Exception as e:
        status = 400
        message = str(rules.InvalidRules(str(e)))
        if hasattr(e, 'problem_mark'):
            message += " (position %s:%s)" % (e.problem_mark.line + 1,
                                              e.problem_mark.column + 1)
    else:
        status = 200
        message = "The configuration is valid"

    return flask.Response(message, status=status, mimetype="text/plain")


@app.route("/refresh/<owner>/<repo>/<path:refresh_ref>", methods=["POST"])
def refresh(owner, repo, refresh_ref):
    authentification()
    github_events.job_refresh.delay(owner, repo, refresh_ref)
    return "Refresh queued", 202


@app.route("/refresh", methods=["POST"])
def refresh_all():
    authentification()
    github_events.job_refresh_all.delay()
    return "Refresh queued", 202


@app.route("/subscription-cache/<installation_id>", methods=["DELETE"])
def subscription_cache(installation_id):  # pragma: no cover
    authentification()
    r = utils.get_redis_for_cache()
    r.delete("subscription-cache-%s" % installation_id)
    return "Cache cleaned", 200


@app.route("/marketplace", methods=["POST"])
def marketplace_handler():
    authentification()
    return "Event ignored", 202


@app.route("/queues/<owner>/<repo>", methods=["GET"])
def queues(owner, repo):
    authentification()

    redis = utils.get_redis_for_cache()
    queues = {}
    filter_ = "strict-merge-queues~*~%s~%s~*" % (owner.lower(), repo.lower())
    for queue in redis.keys(filter_):
        _, _, _, _, branch = queue.split("~")
        queues[branch] = [int(pull) for pull, score in redis.zscan_iter(queue)]

    return flask.jsonify(queues)


@app.route("/event", methods=["POST"])
def event_handler():
    authentification()

    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()

    github_events.job_filter_and_dispatch.apply_async(
        args=[event_type, event_id, data],
        countdown=30
    )
    return "Event queued", 202


# NOTE(sileht): These endpoints are used for recording cassetes, we receive
# Github event on POST, we store them is redis, GET to retreive and delete
@app.route("/events-testing", methods=["POST", "GET", "DELETE"])
def event_testing_handler():  # pragma: no cover
    authentification()
    r = utils.get_redis_for_cache()
    if flask.request.method == "DELETE":
        r.delete("events-testing")
        return "", 202
    elif flask.request.method == "POST":
        event_type = flask.request.headers.get("X-GitHub-Event")
        event_id = flask.request.headers.get("X-GitHub-Delivery")
        data = flask.request.get_json()
        r.rpush("events-testing", json.dumps(
            {"id": event_id, "type": event_type, "payload": data}
        ))
        return "", 202
    else:
        p = r.pipeline()
        number = flask.request.args.get('number')
        if number:
            for _ in range(int(number)):
                p.lpop("events-testing")
            values = p.execute()
        else:
            p.lrange("events-testing", 0, -1)
            p.delete("events-testing")
            values = p.execute()[0]
        data = [json.loads(i) for i in values
                if i is not None]
        return flask.jsonify(data)


@app.route("/")
def index():  # pragma: no cover
    return flask.redirect("https://mergify.io/")
