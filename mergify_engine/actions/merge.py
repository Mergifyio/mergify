# -*- encoding: utf-8 -*-
#
#  Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

import voluptuous

from mergify_engine import actions
from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import utils
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


def merge_report(pull):
    if pull.g_pull.merged:
        if pull.g_pull.merged_by.login == 'mergify[bot]':
            mode = "automatically"
        else:
            mode = "manually"
        conclusion = "success"
        title = "The pull request has been merged %s" % mode
        summary = ("The pull request has been merged %s at *%s*" %
                   (mode, pull.g_pull.merge_commit_sha))
    elif pull.g_pull.state == "closed":
        conclusion = "cancelled"
        title = "The pull request has been closed manually"
        summary = ""
    else:
        return

    return conclusion, title, summary


def output_for_mergeable_state(pull, strict):
    # NOTE(sileht): Take care of all branch protection state
    if pull.g_pull.mergeable_state == "dirty":
        return None, "Merge conflict needs to be solved", ""
    elif pull.g_pull.mergeable_state == "unknown":
        return ("failure", "Pull request state reported as `unknown` by "
                "GitHub", "")
    # FIXME(sileht): We disable this check as github wrongly report
    # mergeable_state == blocked sometimes. The workaround is to try to merge
    # it and if that fail we checks for blocking state.
    # elif pull.g_pull.mergeable_state == "blocked":
    #     return ("failure", "Branch protection settings are blocking "
    #            "automatic merging", "")
    elif (pull.g_pull.mergeable_state == "behind" and not strict):
        # Strict mode has been enabled in branch protection but not in
        # mergify
        return ("failure", "Branch protection setting 'strict' conflicts "
                "with Mergify configuration", "")
        # NOTE(sileht): remaining state "behind, clean, unstable, has_hooks"
        # are OK for us
    return


class MergeAction(actions.Action):
    validator = {
        voluptuous.Required("method", default="merge"):
        voluptuous.Any("rebase", "merge", "squash"),
        voluptuous.Required("rebase_fallback", default="merge"):
        voluptuous.Any("merge", "squash", None),
        voluptuous.Required("strict", default=False):
        voluptuous.Any(bool, "smart"),
        voluptuous.Required("strict_method", default="merge"):
        voluptuous.Any("rebase", "merge")
    }

    def run(self, installation_id, installation_token, subscription,
            event_type, data, pull, missing_conditions):
        pull.log.debug("process merge", config=self.config)

        output = merge_report(pull)
        if output:
            return output

        output = output_for_mergeable_state(pull, self.config["strict"])
        if output:
            return output

        if self.config["strict"] and pull.is_behind():
            if not pull.base_is_modifiable():
                return ("failure", "Pull request can't be updated with latest "
                        "base branch changes, owner doesn't allow "
                        "modification")
            elif self.config["strict"] == "smart":
                key = _get_queue_cache_key(pull)
                redis = utils.get_redis_for_cache()
                redis.sadd(key, pull.g_pull.number)
                redis.set(_get_update_method_cache_key(pull),
                          self.config["strict_method"])
                return (None, "Base branch will be updated soon",
                        "The pull request base branch will "
                        "be updated soon, and then merged.")
            else:
                return update_pull_base_branch(pull, subscription,
                                               self.config["strict_method"])
        else:

            if self.config["strict"] == "smart":
                redis = utils.get_redis_for_cache()
                redis.srem(_get_queue_cache_key(pull), pull.g_pull.number)
                redis.delete(_get_update_method_cache_key(pull))

            if (self.config["method"] != "rebase" or
                    pull.g_pull.raw_data['rebaseable']):
                return self._merge(pull, self.config["method"])
            elif self.config["rebase_fallback"]:
                return self._merge(pull, self.config["rebase_fallback"])
            else:
                return ("action_required", "Automatic rebasing is not "
                        "possible, manual intervention required", "")

    def cancel(self, installation_id, installation_token, subscription,
               event_type, data, pull, missing_conditions):
        # We just rebase the pull request, don't cancel it yet if CIs are
        # running. The pull request will be merge if all rules match again.
        # if not we will delete it when we received all CIs termination
        if self.config["strict"] and self._required_statuses_in_progress(
                pull, missing_conditions):
            return

        if self.config["strict"] == "smart":
            redis = utils.get_redis_for_cache()
            redis.srem(_get_queue_cache_key(pull), pull.g_pull.number)

        return self.cancelled_check_report

    @staticmethod
    def _required_statuses_in_progress(pull, missing_conditions):
        # It's closed, it's not going to change
        if pull.g_pull.state == "closed":
            return False

        need_look_at_checks = []
        for condition in missing_conditions:
            if condition.attribute_name.startswith("status-"):
                need_look_at_checks.append(condition)
            else:
                # something else does not match anymore
                return False

        if need_look_at_checks:
            checks = list(pull._get_checks())
            if not checks:
                # No checks have been send yet
                return True

            # Take only checks we care about
            checks = [s for s in checks
                      for cond in need_look_at_checks
                      if cond(**{cond.attribute_name: s.context})]
            if not checks:
                return True

            for s in checks:
                if s.state in ("pending", None):
                    return True

        return False

    @staticmethod
    def _merge(pull, method):
        try:
            pull.g_pull.merge(sha=pull.g_pull.head.sha,
                              merge_method=method)
        except github.GithubException as e:   # pragma: no cover
            if pull.g_pull.is_merged():
                pull.log.info("merged in the meantime")

            elif e.status == 405:
                pull.log.error("merge fail", error=e.data["message"])
                if pull.g_pull.mergeable_state == "blocked":
                    return ("failure", "Branch protection settings are "
                            "blocking automatic merging", e.data["message"])
                else:
                    return ("failure",
                            "Repository settings are blocking automatic "
                            "merging", e.data["message"])

            elif 400 <= e.status < 500:
                pull.log.error("merge fail", error=e.data["message"])
                return ("failure",
                        "Mergify fails to merge the pull request",
                        e.data["message"])
            else:
                raise
        else:
            pull.log.info("merged")

        pull.g_pull.update()
        return merge_report(pull)


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


def update_pull_base_branch(pull, subscription, method):
    updated = branch_updater.update(pull, subscription["token"], method)
    if updated:
        redis = utils.get_redis_for_cache()
        # NOTE(sileht): We store this for dismissal action
        redis.setex("branch-update-%s" % updated, 60 * 60, updated)

        # NOTE(sileht): We update g_pull to have the new head.sha,
        # so future created checks will be posted on the new sha.
        # Otherwise the checks will be lost the GitHub UI on the
        # old sha.
        pull.wait_for_sha_change()
        return (None, "Base branch updates done",
                "The pull request has been automatically "
                "updated to follow its base branch and will be "
                "merged soon")
    else:
        # NOTE(sileht): Maybe the PR have been rebased and/or merged manually
        # in the meantime. So double check that to not report a wrong status
        pull.g_pull.update()
        output = merge_report(pull)
        if output:
            return output
        else:
            return ("failure", "Base branch update has failed", "")


def update_next_pull(installation_id, installation_token, subscription,
                     owner, reponame, branch, key, cur_key):
    redis = utils.get_redis_for_cache()
    pull_number = redis.srandmember(key)
    if not pull_number:
        LOG.debug("no more pull request to update",
                  installation_id=installation_id,
                  pull_number=pull_number,
                  repo=owner + "/" + reponame, branch=branch)
        return

    LOG.debug("next pull to rebase",
              installation_id=installation_id,
              pull_number=pull_number,
              repo=owner + "/" + reponame, branch=branch)

    pull = mergify_pull.MergifyPull.from_number(
        installation_id, installation_token,
        owner, reponame, int(pull_number))

    old_checks = [c for c in check_api.get_checks(pull.g_pull)
                  if (c.name.endswith(" (merge)") and
                      c._rawData['app']['id'] == config.INTEGRATION_ID)]

    merge_output = merge_report(pull)
    mergeable_state_output = output_for_mergeable_state(pull, True)
    if merge_output:
        redis.srem(_get_queue_cache_key(pull), pull.g_pull.number)
        conclusion, title, summary = merge_output
    elif mergeable_state_output:
        redis.srem(_get_queue_cache_key(pull), pull.g_pull.number)
        conclusion, title, summary = mergeable_state_output
    else:
        method = redis.get(_get_update_method_cache_key(pull)) or "merge"
        conclusion, title, summary = update_pull_base_branch(
            pull, subscription, method)
        if pull.g_pull.state == "closed":
            redis.srem(_get_queue_cache_key(pull), pull.g_pull.number)
        else:
            redis.set(cur_key, pull_number)

    status = "completed" if conclusion else "in_progress"
    for c in old_checks:
        check_api.set_check_run(
            pull.g_pull, c.name, status, conclusion,
            output={"title": title, "summary": summary})


@app.task
def smart_strict_workflow_periodic_task():
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    redis = utils.get_redis_for_cache()
    LOG.debug("smart strict workflow loop start")
    for key in redis.keys("strict-merge-queues~*"):
        _, installation_id, owner, reponame, branch = key.split("~")
        try:
            installation_token = integration.get_access_token(
                installation_id).token
        except github.UnknownObjectException:  # pragma: no cover
            LOG.error("token for install %d does not exists anymore (%s/%s)",
                      installation_id, owner, reponame)
            continue

        cur_key = "current-%s" % key
        redis = utils.get_redis_for_cache()
        pull_number = redis.get(cur_key)
        if pull_number and redis.sismember(key, pull_number):
            pull = mergify_pull.MergifyPull.from_number(
                installation_id, installation_token,
                owner, reponame, int(pull_number))

            if pull.g_pull.state == "closed" or pull.is_behind():
                # NOTE(sileht): Someone can have merged something manually in
                # base branch in the meantime, so we have to update it again.
                LOG.debug("pull request needs to be updated again or "
                          "has been closed",
                          installation_id=installation_id,
                          pull_number=pull_number,
                          repo=owner + "/" + reponame, branch=branch)
            else:
                # NOTE(sileht): Pull request has not been merged or cancelled
                # yet wait next loop
                LOG.debug("pull request checks are still in progress",
                          installation_id=installation_id,
                          pull_number=pull_number,
                          repo=owner + "/" + reponame, branch=branch)
                continue

        subscription = utils.get_subscription(redis, installation_id)
        if not subscription["token"]:  # pragma: no cover
            LOG.error("no subscription token for updating base branch",
                      installation_id=installation_id,
                      repo=owner + "/" + reponame, branch=branch)
            continue

        # NOTE(sileht): Pick up the next pull request and rebase it
        update_next_pull(installation_id, installation_token, subscription,
                         owner, reponame, branch, key, cur_key)
    LOG.debug("smart strict workflow loop end")
