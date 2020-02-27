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

import daiquiri
import github

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import mergify_pull
from mergify_engine import utils
from mergify_engine.actions.merge import helpers
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


def get_queue_logger(queue):
    _, installation_id, owner, reponame, branch = queue.split("~")
    return daiquiri.getLogger(
        __name__, gh_owner=owner, gh_repo=reponame, gh_branch=branch
    )


def _get_queue_cache_key(pull):
    return "strict-merge-queues~%s~%s~%s~%s" % (
        pull.installation_id,
        pull.base_repo_owner_login.lower(),
        pull.base_repo_name.lower(),
        pull.base_ref,
    )


def _get_update_method_cache_key(pull):
    return "strict-merge-method~%s~%s~%s~%s" % (
        pull.installation_id,
        pull.base_repo_owner_login.lower(),
        pull.base_repo_name.lower(),
        pull.number,
    )


def add_pull(pull, method):
    queue = _get_queue_cache_key(pull)
    redis = utils.get_redis_for_cache()
    score = utils.utcnow().timestamp()
    redis.zadd(queue, {pull.number: score}, nx=True)
    redis.set(_get_update_method_cache_key(pull), method)
    pull.log.debug("pull request added to merge queue", queue=queue)


def remove_pull(pull):
    redis = utils.get_redis_for_cache()
    queue = _get_queue_cache_key(pull)
    redis.zrem(queue, pull.number)
    redis.delete(_get_update_method_cache_key(pull))
    pull.log.debug("pull request removed from merge queue", queue=queue)


def _move_pull_at_end(pull):  # pragma: no cover
    redis = utils.get_redis_for_cache()
    queue = _get_queue_cache_key(pull)
    score = utils.utcnow().timestamp()
    redis.zadd(queue, {pull.number: score}, xx=True)
    pull.log.debug(
        "pull request moved at the end of the merge queue", queue=queue,
    )


def _get_pulls(queue):
    redis = utils.get_redis_for_cache()
    return redis.zrange(queue, 0, -1)


def get_pulls_from_queue(pull):
    queue = _get_queue_cache_key(pull)
    return _get_pulls(queue)


def _get_next_pull_request(queue, queue_log):
    _, installation_id, owner, reponame, branch = queue.split("~")

    integration = github.GithubIntegration(config.INTEGRATION_ID, config.PRIVATE_KEY)
    try:
        installation_token = integration.get_access_token(installation_id).token
    except github.UnknownObjectException:  # pragma: no cover
        LOG.error(
            "token for install %d does not exists anymore",
            installation_id,
            gh_owner=owner,
            gh_repo=reponame,
        )
        return

    pull_numbers = _get_pulls(queue)
    queue_log.debug("%d pulls queued", len(pull_numbers), queue=list(pull_numbers))
    if pull_numbers:
        return mergify_pull.MergifyPull.from_number(
            installation_id, installation_token, owner, reponame, int(pull_numbers[0])
        )


def _handle_first_pull_in_queue(queue, pull):
    _, installation_id, owner, reponame, branch = queue.split("~")
    old_checks = [
        c
        for c in check_api.get_checks(pull.g_pull, mergify_only=True)
        if c.name.endswith(" (merge)")
    ]

    output = helpers.merge_report(pull, True)
    if output:
        conclusion, title, summary = output
        pull.log.debug(
            "pull request closed in the meantime",
            conclusion=conclusion,
            title=title,
            summary=summary,
        )
        remove_pull(pull)
    else:
        pull.log.debug("updating base branch of pull request")
        redis = utils.get_redis_for_cache()
        method = redis.get(_get_update_method_cache_key(pull)) or "merge"
        conclusion, title, summary = helpers.update_pull_base_branch(pull, method)

        if pull.state == "closed":
            pull.log.debug(
                "pull request closed in the meantime",
                conclusion=conclusion,
                title=title,
                summary=summary,
            )
            remove_pull(pull)
        elif conclusion == "failure":
            pull.log.debug("base branch update failed", title=title, summary=summary)
            _move_pull_at_end(pull)

    status = "completed" if conclusion else "in_progress"
    for c in old_checks:
        check_api.set_check_run(
            pull.g_pull,
            c.name,
            status,
            conclusion,
            output={"title": title, "summary": summary},
        )


@app.task
def smart_strict_workflow_periodic_task():
    # NOTE(sileht): Don't use the celery retry mechnism here, the
    # periodic tasks already retries. This ensure a repo can't block
    # another one.

    redis = utils.get_redis_for_cache()
    LOG.debug("smart strict workflow loop start")
    for queue in redis.keys("strict-merge-queues~*"):
        queue_log = get_queue_logger(queue)
        queue_log.debug("handling queue: %s", queue)

        pull = None
        try:
            pull = _get_next_pull_request(queue, queue_log)
            if not pull:
                queue_log.debug("no pull request for this queue")
            elif pull.state == "closed" or pull.is_behind:
                # NOTE(sileht): Pick up this pull request and rebase it again
                # or update its status and remove it from the queue
                pull.log.debug(
                    "pull request needs to be updated again or has been closed",
                )
                _handle_first_pull_in_queue(queue, pull)
            else:
                # NOTE(sileht): Pull request has not been merged or cancelled
                # yet wait next loop
                pull.log.debug("pull request checks are still in progress")

        except exceptions.RateLimited as e:
            log = pull.log if pull else queue_log
            log.info("rate limited", remaining_seconds=e.countdown)
        except exceptions.MergeableStateUnknown as e:  # pragma: no cover
            e.pull.log.warning(
                "pull request with mergeable_state unknown retrying later",
            )
            _move_pull_at_end(e.pull)
        except Exception:  # pragma: no cover
            log = pull.log if pull else queue_log
            log.error(
                "Fail to process merge queue", exc_info=True,
            )
            if pull:
                _move_pull_at_end(pull)

    LOG.debug("smart strict workflow loop end")
