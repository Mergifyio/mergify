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

import collections
import hmac
import json
import logging
from urllib.parse import urlsplit

import flask
from flask_cors import cross_origin
import github
import voluptuous

from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import rules
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.tasks import forward_events
from mergify_engine.tasks import github_events
from mergify_engine.tasks import mergify_events
from mergify_engine.tasks.engine import actions_runner


LOG = logging.getLogger(__name__)

app = flask.Flask(__name__, static_url_path="")


def authentification():  # pragma: no cover
    # Only SHA1 is supported
    header_signature = flask.request.headers.get("X-Hub-Signature")
    if header_signature is None:
        LOG.warning("Webhook without signature")
        flask.abort(403)

    try:
        sha_name, signature = header_signature.split("=")
    except ValueError:
        sha_name = None

    if sha_name != "sha1":
        LOG.warning("Webhook signature malformed")
        flask.abort(403)

    mac = utils.compute_hmac(flask.request.data)
    if not hmac.compare_digest(mac, str(signature)):
        LOG.warning("Webhook signature invalid")
        flask.abort(403)


def _get_badge_url(owner, repo, ext):
    style = flask.request.args.get("style", "flat")
    return flask.redirect(
        f"https://img.shields.io/endpoint.{ext}?url=https://dashboard.mergify.io/badges/{owner}/{repo}&style={style}"
    )


@app.route("/badges/<owner>/<repo>.png")
def badge_png(owner, repo):  # pragma: no cover
    return _get_badge_url(owner, repo, "png")


@app.route("/badges/<owner>/<repo>.svg")
def badge_svg(owner, repo):  # pragma: no cover
    return _get_badge_url(owner, repo, "svg")


@app.route("/badges/<owner>/<repo>")
def badge(owner, repo):
    return flask.redirect(f"https://dashboard.mergify.io/badges/{owner}/{repo}")


@app.route("/validate", methods=["POST"])
@cross_origin()
def config_validator():  # pragma: no cover
    try:
        rules.UserConfigurationSchema(flask.request.files["data"].stream)
    except Exception as e:
        status = 400
        message = str(e)
    else:
        status = 200
        message = "The configuration is valid"

    return flask.Response(message, status=status, mimetype="text/plain")


@app.route("/refresh/<owner>/<repo>", methods=["POST"])
def refresh_repo(owner, repo):
    authentification()
    mergify_events.job_refresh.delay(owner, repo, "repo")
    return "Refresh queued", 202


RefreshActionSchema = voluptuous.Schema(voluptuous.Any("user", "forced"))


@app.route("/refresh/<owner>/<repo>/pull/<int:pull>", methods=["POST"])
def refresh_pull(owner, repo, pull):
    authentification()
    action = RefreshActionSchema(flask.request.args.get("action", "user"))
    mergify_events.job_refresh.delay(owner, repo, "pull", pull, action=action)
    return "Refresh queued", 202


@app.route("/refresh/<owner>/<repo>/branch/<branch>", methods=["POST"])
def refresh_branch(owner, repo, branch):
    authentification()
    mergify_events.job_refresh.delay(owner, repo, "branch", branch)
    return "Refresh queued", 202


@app.route("/subscription-cache/<installation_id>", methods=["PUT"])
def subscription_cache_update(installation_id):  # pragma: no cover
    authentification()
    r = utils.get_redis_for_cache()
    sub = flask.request.get_json()
    if sub is None:
        return "Empty content", 400
    sub_utils.save_subscription_to_cache(r, installation_id, sub)
    return "Cache updated", 200


@app.route("/subscription-cache/<installation_id>", methods=["DELETE"])
def subscription_cache_delete(installation_id):  # pragma: no cover
    authentification()
    r = utils.get_redis_for_cache()
    r.delete("subscription-cache-%s" % installation_id)
    return "Cache cleaned", 200


class PullRequestUrlInvalid(voluptuous.Invalid):
    pass


@voluptuous.message("expected a Pull Request URL", cls=PullRequestUrlInvalid)
def PullRequestUrl(v):
    _, owner, repo, _, pull_number = urlsplit(v).path.split("/")
    pull_number = int(pull_number)

    integration = github.GithubIntegration(config.INTEGRATION_ID, config.PRIVATE_KEY)
    try:
        installation_id = utils.get_installation_id(integration, owner, repo)
    except github.GithubException:
        raise PullRequestUrlInvalid(
            message="Mergify not installed on repository '%s'" % owner
        )

    token = integration.get_access_token(installation_id).token
    try:
        return mergify_pull.MergifyPull.from_number(
            installation_id, token, owner, repo, pull_number
        )
    except github.UnknownObjectException:
        raise PullRequestUrlInvalid(message=("Pull request '%s' not found" % v))


SimulatorSchema = voluptuous.Schema(
    {
        voluptuous.Required("pull_request"): voluptuous.Any(None, PullRequestUrl()),
        voluptuous.Required("mergify.yml"): rules.UserConfigurationSchema,
    }
)


def ensure_no_voluptuous(value):
    if isinstance(value, (dict, list, str)):
        return value
    else:
        return str(value)


def voluptuous_error(error):
    return {
        "type": error.__class__.__name__,
        "message": str(error),
        "error": error.msg,
        "details": list(map(ensure_no_voluptuous, error.path)),
    }


@app.errorhandler(voluptuous.Invalid)
def voluptuous_errors(error):
    # FIXME(sileht): remove error at payload root
    payload = voluptuous_error(error)
    payload["errors"] = []
    if isinstance(error, voluptuous.MultipleInvalid):
        payload["errors"].extend(map(voluptuous_error, error.errors))
    else:
        payload["errors"].extend(voluptuous_error(error))
    return flask.make_response(flask.jsonify(payload), 400)


@app.route("/simulator", methods=["POST"])
def simulator():
    authentification()

    data = SimulatorSchema(flask.request.get_json(force=True))
    pull_request = data["pull_request"]

    if pull_request:
        pull_request_rules = data["mergify.yml"]["pull_request_rules"]

        match = pull_request_rules.get_pull_request_rule(pull_request)

        raw_event = {
            "repository": pull_request.g_pull.base.repo.raw_data,
            "installation": {"id": pull_request.installation_id},
            "pull_request": pull_request.g_pull.raw_data,
        }
        title, summary = actions_runner.gen_summary(
            pull_request, [{"event_type": "refresh", "data": raw_event}], match
        )
    else:
        title = "The configuration is valid"
        summary = None
    return flask.jsonify({"title": title, "summary": summary}), 200


@app.route("/marketplace", methods=["POST"])
def marketplace_handler():  # pragma: no cover
    authentification()

    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()

    github_events.job_marketplace.apply_async(args=[event_type, event_id, data])

    if config.WEBHOOK_MARKETPLACE_FORWARD_URL:
        forward_events.post.s(
            config.WEBHOOK_MARKETPLACE_FORWARD_URL,
            data=flask.request.get_data().decode(),
            headers={
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": event_id,
                "X-Hub-Signature": flask.request.headers.get("X-Hub-Signature"),
                "User-Agent": flask.request.headers.get("User-Agent"),
                "Content-Type": flask.request.headers.get("Content-Type"),
            },
        ).delay()

    return "Event queued", 202


@app.route("/queues/<installation_id>", methods=["GET"])
def queues(installation_id):
    authentification()

    redis = utils.get_redis_for_cache()
    queues = collections.defaultdict(dict)
    filter_ = "strict-merge-queues~%s~*" % installation_id
    for queue in redis.keys(filter_):
        _, _, owner, repo, branch = queue.split("~")
        queues[owner + "/" + repo][branch] = [
            int(pull) for pull, score in redis.zscan_iter(queue)
        ]

    return flask.jsonify(queues)


@app.route("/event", methods=["POST"])
def event_handler():
    authentification()

    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()

    github_events.job_filter_and_dispatch.apply_async(
        args=[event_type, event_id, data], countdown=30
    )

    if (
        config.WEBHOOK_APP_FORWARD_URL
        and config.WEBHOOK_FORWARD_EVENT_TYPES is not None
        and event_type in config.WEBHOOK_FORWARD_EVENT_TYPES
    ):
        forward_events.post.s(
            config.WEBHOOK_APP_FORWARD_URL,
            data=flask.request.get_data().decode(),
            headers={
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": event_id,
                "X-Hub-Signature": flask.request.headers.get("X-Hub-Signature"),
                "User-Agent": flask.request.headers.get("User-Agent"),
                "Content-Type": flask.request.headers.get("Content-Type"),
            },
        ).delay()

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
        r.rpush(
            "events-testing",
            json.dumps({"id": event_id, "type": event_type, "payload": data}),
        )
        return "", 202
    else:
        p = r.pipeline()
        number = flask.request.args.get("number")
        if number:
            for _ in range(int(number)):
                p.lpop("events-testing")
            values = p.execute()
        else:
            p.lrange("events-testing", 0, -1)
            p.delete("events-testing")
            values = p.execute()[0]
        data = [json.loads(i) for i in values if i is not None]
        return flask.jsonify(data)


@app.route("/marketplace-testing", methods=["POST"])
def marketplace_testng_handler():  # pragma: no cover
    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()
    LOG.debug(
        "received marketplace testing events",
        event_type=event_type,
        event_id=event_id,
        data=data,
    )
    return "Event ignored", 202


@app.route("/")
def index():  # pragma: no cover
    return flask.redirect("https://mergify.io/")
