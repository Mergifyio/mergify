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


def _get_queue_cache_key(pull):
    return "strict-merge-queues~%s~%s~%s~%s" % (
        pull.installation_id,
        pull.g_pull.base.repo.owner.login.lower(),
        pull.g_pull.base.repo.name.lower(),
        pull.g_pull.base.ref
    )


def _get_update_method_cache_key(pull):
    return "strict-merge-method~%s~%s~%s~%s" % (
        pull.installation_id,
        pull.g_pull.base.repo.owner.login.lower(),
        pull.g_pull.base.repo.name.lower(),
        pull.g_pull.number,
    )


def add_pull(pull, method):
    queue = _get_queue_cache_key(pull)
    redis = utils.get_redis_for_cache()
    score = utils.utcnow().timestamp()
    redis.zadd(queue, {pull.g_pull.number: score}, nx=True)
    redis.set(_get_update_method_cache_key(pull), method)
    LOG.debug("pull request added to merge queue", queue=queue, pull=pull)


def remove_pull(pull):
    redis = utils.get_redis_for_cache()
    queue = _get_queue_cache_key(pull)
    redis.zrem(queue, pull.g_pull.number)
    redis.delete(_get_update_method_cache_key(pull))
    LOG.debug("pull request removed from merge queue", queue=queue, pull=pull)


def _move_pull_at_end(pull):  # pragma: no cover
    redis = utils.get_redis_for_cache()
    queue = _get_queue_cache_key(pull)
    score = utils.utcnow().timestamp()
    redis.zadd(queue, {pull.g_pull.number: score}, xx=True)
    LOG.debug("pull request moved at the end of the merge queue",
              queue=queue, pull_request=pull)


def _get_next_pull_request(queue):
    _, installation_id, owner, reponame, branch = queue.split("~")

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    try:
        installation_token = integration.get_access_token(
            installation_id).token
    except github.UnknownObjectException:  # pragma: no cover
        LOG.error("token for install %d does not exists anymore (%s/%s)",
                  installation_id, owner, reponame)
        return

    redis = utils.get_redis_for_cache()
    pull_numbers = redis.zrange(queue, 0, 0)
    if pull_numbers:
        return mergify_pull.MergifyPull.from_number(
            installation_id, installation_token,
            owner, reponame, int(pull_numbers[0]))


def _handle_first_pull_in_queue(queue, pull):
    _, installation_id, owner, reponame, branch = queue.split("~")
    old_checks = [c for c in check_api.get_checks(pull.g_pull)
                  if (c.name.endswith(" (merge)") and
                      c._rawData['app']['id'] == config.INTEGRATION_ID)]

    merge_output = helpers.merge_report(pull)
    mergeable_state_output = helpers.output_for_mergeable_state(pull, True)
    if merge_output or mergeable_state_output:
        conclusion, title, summary = merge_output or mergeable_state_output
        LOG.debug("pull request closed in the meantime", pull=pull,
                  conclusion=conclusion, title=title, summary=summary)
        remove_pull(pull)
    else:
        LOG.debug("updating base branch of pull request", pull=pull)
        redis = utils.get_redis_for_cache()
        method = redis.get(_get_update_method_cache_key(pull)) or "merge"
        conclusion, title, summary = helpers.update_pull_base_branch(
            pull, installation_id, method)

        if pull.g_pull.state == "closed":
            LOG.debug("pull request closed in the meantime", pull=pull,
                      conclusion=conclusion, title=title, summary=summary)
            remove_pull(pull)
        elif conclusion == "failure":
            LOG.debug("base branch update failed", pull=pull, title=title,
                      summary=summary)
            _move_pull_at_end(pull)

    status = "completed" if conclusion else "in_progress"
    for c in old_checks:
        check_api.set_check_run(
            pull.g_pull, c.name, status, conclusion,
            output={"title": title, "summary": summary})


@app.task
def smart_strict_workflow_periodic_task():
    # NOTE(sileht): Don't use the celery retry mechnism here, the
    # periodic tasks already retries. This ensure a repo can't block
    # another one.

    redis = utils.get_redis_for_cache()
    LOG.debug("smart strict workflow loop start")
    for queue in redis.keys("strict-merge-queues~*"):
        LOG.debug("handling queue: %s", queue)

        pull = None
        try:
            pull = _get_next_pull_request(queue)
            if not pull:
                LOG.debug("no pull request for this queue", queue=queue)
            elif pull.g_pull.state == "closed" or pull.is_behind():
                # NOTE(sileht): Pick up this pull request and rebase it again
                # or update its status and remove it from the queue
                LOG.debug("pull request needs to be updated again or "
                          "has been closed", pull_request=pull)
                _handle_first_pull_in_queue(queue, pull)
            else:
                # NOTE(sileht): Pull request has not been merged or cancelled
                # yet wait next loop
                LOG.debug("pull request checks are still in progress",
                          pull_request=pull)

        except exceptions.MergeableStateUnknown as e:  # pragma: no cover
            LOG.warning("pull request with mergeable_state unknown "
                        "retrying later", pull_request=e.pull)
            _move_pull_at_end(e.pull)
        except Exception:  # pragma: no cover
            LOG.error("Fail to process merge queue", queue=queue,
                      pull_request=pull, exc_info=True)
            if pull:
                _move_pull_at_end(pull)

    LOG.debug("smart strict workflow loop end")
