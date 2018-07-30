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

import rq_dashboard

from mergify_engine import queue
from mergify_engine import utils


LOG = logging.getLogger(__name__)

app = flask.Flask(__name__)

app.config.from_object(rq_dashboard.default_settings)
app.register_blueprint(rq_dashboard.blueprint, url_prefix="/rq")
app.config["REDIS_URL"] = utils.get_redis_url()
app.config["RQ_POLL_INTERVAL"] = 10000  # ms


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


@app.route("/check_status_msg/<path:key>")
def check_status_msg(key):
    msg = utils.get_redis_for_cache().hget("status", key)
    if msg:
        return flask.render_template("msg.html", msg=msg)
    else:
        flask.abort(404)


@app.route("/refresh/<owner>/<repo>/<path:refresh_ref>", methods=["POST"])
def refresh(owner, repo, refresh_ref):
    authentification()
    queue.publish("refresh", owner, repo, refresh_ref)
    return "Refresh queued", 202


@app.route("/refresh", methods=["POST"])
def refresh_all():
    authentification()
    queue.publish("refresh_all")
    return "Refresh queued", 202


# FIXME(sileht): rename this to new subscription something
@app.route("/subscription-cache/<installation_id>", methods=["DELETE"])
def subscription_cache(installation_id):  # pragma: no cover
    authentification()
    r = utils.get_redis_for_cache()
    r.delete("subscription-cache-%s" % installation_id)

    subscription = utils.get_subscription(
        utils.get_redis_for_cache(), installation_id)

    # New subscription, create initial configuration for private repo
    # public repository have already been done during the installation
    # event.
    if subscription["token"] and subscription["subscribed"]:
        # FIXME(sileht): We should pass the slugs
        queue.push(installation_id, subscription, "installations",
                   installation_id, "private")
    return "Cache cleaned", 200


@app.route("/event", methods=["POST"])
def event_handler():
    authentification()

    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()

    queue.publish("filter_and_dispatch", event_type, event_id, data)
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
        p.lrange("events-testing", 0, -1)
        p.delete("events-testing")
        values = p.execute()[0]
        data = [json.loads(i) for i in values]
        return flask.jsonify(data)


@app.route("/")
def index():  # pragma: no cover
    return flask.redirect("https://mergify.io/")
