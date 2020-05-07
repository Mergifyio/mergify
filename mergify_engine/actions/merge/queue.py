# -*- encoding: utf-8 -*-
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

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.actions.merge import helpers
from mergify_engine.clients import github


LOG = logs.getLogger(__name__)


def get_queue_logger(queue):
    _, installation_id, owner, reponame, branch = queue.split("~")
    return logs.getLogger(__name__, gh_owner=owner, gh_repo=reponame, gh_branch=branch)


def _get_queue_cache_key(ctxt, base_ref=None):
    return "strict-merge-queues~%s~%s~%s~%s" % (
        ctxt.client.installation["id"],
        ctxt.pull["base"]["repo"]["owner"]["login"].lower(),
        ctxt.pull["base"]["repo"]["name"].lower(),
        base_ref or ctxt.pull["base"]["ref"],
    )


def _get_update_method_cache_key(ctxt):
    return "strict-merge-method~%s~%s~%s~%s" % (
        ctxt.client.installation["id"],
        ctxt.pull["base"]["repo"]["owner"]["login"].lower(),
        ctxt.pull["base"]["repo"]["name"].lower(),
        ctxt.pull["number"],
    )


def add_pull(ctxt, method):
    queue = _get_queue_cache_key(ctxt)
    redis = utils.get_redis_for_cache()
    score = utils.utcnow().timestamp()
    redis.zadd(queue, {ctxt.pull["number"]: score}, nx=True)
    redis.set(_get_update_method_cache_key(ctxt), method)
    ctxt.log.info("pull request added to merge queue", queue=queue)


def remove_pull(ctxt):
    redis = utils.get_redis_for_cache()
    queue = _get_queue_cache_key(ctxt)
    redis.zrem(queue, ctxt.pull["number"])
    redis.delete(_get_update_method_cache_key(ctxt))
    ctxt.log.info("pull request removed from merge queue", queue=queue)


def _move_pull_at_end(ctxt):  # pragma: no cover
    redis = utils.get_redis_for_cache()
    queue = _get_queue_cache_key(ctxt)
    score = utils.utcnow().timestamp()
    redis.zadd(queue, {ctxt.pull["number"]: score}, xx=True)
    ctxt.log.info(
        "pull request moved at the end of the merge queue", queue=queue,
    )


def _move_pull_to_new_base_branch(ctxt, old_base_branch):
    redis = utils.get_redis_for_cache()
    old_queue = _get_queue_cache_key(ctxt, old_base_branch)
    new_queue = _get_queue_cache_key(ctxt)
    redis.zrem(old_queue, ctxt.pull["number"])
    method = redis.get(_get_update_method_cache_key(ctxt)) or "merge"
    add_pull(ctxt, method)
    ctxt.log.info("pull request moved from %s to %s", old_queue, new_queue)


def _get_pulls(queue):
    redis = utils.get_redis_for_cache()
    return redis.zrange(queue, 0, -1)


def get_pulls_from_queue(ctxt):
    queue = _get_queue_cache_key(ctxt)
    return _get_pulls(queue)


def _delete_queue(queue):
    redis = utils.get_redis_for_cache()
    redis.delete(queue)


def _handle_first_pull_in_queue(queue, ctxt):
    _, installation_id, owner, reponame, branch = queue.split("~")
    old_checks = [
        c for c in ctxt.pull_engine_check_runs if c["name"].endswith(" (merge)")
    ]

    output = helpers.merge_report(ctxt, True)
    if output:
        conclusion, title, summary = output
        ctxt.log.info(
            "pull request closed in the meantime",
            conclusion=conclusion,
            title=title,
            summary=summary,
        )
        remove_pull(ctxt)
    else:
        ctxt.log.info("updating base branch of pull request")
        redis = utils.get_redis_for_cache()
        method = redis.get(_get_update_method_cache_key(ctxt)) or "merge"
        conclusion, title, summary = helpers.update_pull_base_branch(ctxt, method)

        if ctxt.pull["state"] == "closed":
            ctxt.log.info(
                "pull request closed in the meantime",
                conclusion=conclusion,
                title=title,
                summary=summary,
            )
            remove_pull(ctxt)
        elif conclusion == "failure":
            ctxt.log.info("base branch update failed", title=title, summary=summary)
            _move_pull_at_end(ctxt)

    status = "completed" if conclusion else "in_progress"
    for c in old_checks:
        check_api.set_check_run(
            ctxt,
            c["name"],
            status,
            conclusion,
            output={"title": title, "summary": summary},
        )


def process_queues():
    # NOTE(sileht): Don't use the celery retry mechnism here, the
    # periodic tasks already retries. This ensure a repo can't block
    # another one.
    redis = utils.get_redis_for_cache()
    LOG.info("smart strict workflow loop start")
    for queue in redis.keys("strict-merge-queues~*"):
        queue_log = get_queue_logger(queue)
        queue_log.info("handling queue: %s", queue)
        try:
            process_queue(queue)
        except Exception:
            queue_log.error("Fail to process merge queue", exc_info=True)
    LOG.info("smart strict workflow loop end")


def process_queue(queue):
    queue_log = get_queue_logger(queue)
    _, installation_id, owner, repo, queue_base_branch = queue.split("~")

    pull_numbers = _get_pulls(queue)

    queue_log.info("%d pulls queued", len(pull_numbers), queue=list(pull_numbers))
    if not pull_numbers:
        return

    pull_number = int(pull_numbers[0])

    try:
        installation = github.get_installation(owner, repo, int(installation_id))
    except exceptions.MergifyNotInstalled:
        _delete_queue(queue)
        return

    subscription = sub_utils.get_subscription(
        utils.get_redis_for_cache(), installation["id"]
    )

    with github.get_client(owner, repo, installation) as client:
        data = client.item(f"pulls/{pull_number}")

        try:
            ctxt = context.Context(client, data, subscription)
        except exceptions.RateLimited as e:
            queue_log.debug("rate limited", remaining_seconds=e.countdown)
            return
        except exceptions.MergeableStateUnknown as e:  # pragma: no cover
            e.ctxt.log.warning(
                "pull request with mergeable_state unknown retrying later",
            )
            _move_pull_at_end(e.ctxt)
            return
        try:
            if ctxt.pull["base"]["ref"] != queue_base_branch:
                ctxt.log.info(
                    "pull request base branch have changed",
                    old_branch=queue_base_branch,
                    new_branch=ctxt.pull["base"]["ref"],
                )
                _move_pull_to_new_base_branch(ctxt, queue_base_branch)
            elif ctxt.pull["state"] == "closed" or ctxt.is_behind:
                # NOTE(sileht): Pick up this pull request and rebase it again
                # or update its status and remove it from the queue
                ctxt.log.info(
                    "pull request needs to be updated again or has been closed",
                )
                _handle_first_pull_in_queue(queue, ctxt)
            else:
                # NOTE(sileht): Pull request has not been merged or cancelled
                # yet wait next loop
                ctxt.log.info("pull request checks are still in progress")
        except Exception:  # pragma: no cover
            ctxt.log.error("Fail to process merge queue", exc_info=True)
            _move_pull_at_end(ctxt)
