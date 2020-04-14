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
import httpx
import pkg_resources
import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.engine import actions_runner
from mergify_engine.engine import commands_runner


LOG = daiquiri.getLogger(__name__)

mergify_rule_path = pkg_resources.resource_filename(
    __name__, "../data/default_pull_request_rules.yml"
)

with open(mergify_rule_path, "r") as f:
    MERGIFY_RULE = yaml.safe_load(f.read())


def get_github_pull_from_sha(client, sha):
    for pull in client.items("pulls"):
        if pull["head"]["sha"] == sha:
            return pull


def get_github_pull_from_event(client, event_type, data):
    if "pull_request" in data and data["pull_request"]:
        return data["pull_request"]

    elif event_type == "issue_comment":
        try:
            return client.item(f"pulls/{data['issue']['number']}")
        except httpx.HTTPNotFound:  # pragma: no cover
            return

    elif event_type == "status":
        return get_github_pull_from_sha(client, data["sha"])

    elif event_type in ["check_suite", "check_run"]:
        # NOTE(sileht): This list may contains Pull Request from another org/user fork...
        base_repo_url = str(client.base_url)[:-1]
        pulls = data[event_type]["pull_requests"]
        pulls = [p for p in pulls if p["base"]["repo"]["url"] == base_repo_url]

        if not pulls:
            sha = data[event_type]["head_sha"]
            pulls = [get_github_pull_from_sha(client, sha)]

        if len(pulls) > 1:  # pragma: no cover
            # NOTE(sileht): It's that technically possible, but really ?
            LOG.warning(
                "check_suite/check_run attached on multiple pulls",
                gh_owner=client.owner,
                gh_repo=client.repo,
                event_type=event_type,
            )

        # NOTE(sileht): data here is really incomplete most of the time,
        # but mergify will complete it automatically if mergeable_state is missing
        if pulls:
            return pulls[0]


def check_configuration_changes(ctxt):
    if ctxt.pull["base"]["repo"]["default_branch"] == ctxt.pull["base"]["ref"]:
        ref = None
        for f in ctxt.files:
            if f["filename"] in rules.MERGIFY_CONFIG_FILENAMES:
                ref = f["contents_url"].split("?ref=")[1]

        if ref is not None:
            try:
                rules.get_mergify_config(ctxt, ref=ref)
            except rules.InvalidRules as e:  # pragma: no cover
                # Not configured, post status check with the error message
                # TODO(sileht): we can annotate the .mergify.yml file in Github
                # UI with that API
                check_api.set_check_run(
                    ctxt,
                    actions_runner.SUMMARY_NAME,
                    "completed",
                    "failure",
                    output={
                        "title": "The new Mergify configuration is invalid",
                        "summary": str(e),
                    },
                )
            else:
                check_api.set_check_run(
                    ctxt,
                    actions_runner.SUMMARY_NAME,
                    "completed",
                    "success",
                    output={
                        "title": "The new Mergify configuration is valid",
                        "summary": "This pull request must be merged "
                        "manually because it modifies Mergify configuration",
                    },
                )

            return True
    return False


def copy_summary_from_previous_head_sha(ctxt, sha):
    checks = check_api.get_checks_for_ref(
        ctxt, sha, check_name=actions_runner.SUMMARY_NAME,
    )
    checks = [c for c in checks if c["app"]["id"] == config.INTEGRATION_ID]

    if not checks:
        ctxt.log.warning(
            "Got synchronize event but didn't find Summary on previous head sha",
        )
        return
    check_api.set_check_run(
        ctxt,
        actions_runner.SUMMARY_NAME,
        "completed",
        "success",
        output={
            "title": checks[0]["output"]["title"],
            "summary": checks[0]["output"]["summary"],
        },
    )


def run(client, pull, sources):
    ctxt = context.Context(client, pull)

    issue_comment_sources = []

    for source in sources:
        if source["event_type"] == "issue_comment":
            issue_comment_sources.append(source)
        else:
            ctxt.sources.append(source)

    commands_runner.spawn_pending_commands_tasks(ctxt)

    if issue_comment_sources:
        for source in issue_comment_sources:
            commands_runner.handle(
                ctxt,
                source["data"]["comment"]["body"],
                source["data"]["comment"]["user"],
            )

    if not ctxt.sources:
        return

    if ctxt.client.installation["permissions_need_to_be_updated"]:
        check_api.set_check_run(
            ctxt,
            "Summary",
            "completed",
            "failure",
            output={
                "title": "Required GitHub permissions are missing.",
                "summary": "You can accept them at https://dashboard.mergify.io/",
            },
        )
        return

    if check_configuration_changes(ctxt):
        ctxt.log.info("Configuration changed, ignoring")
        return

    # BRANCH CONFIGURATION CHECKING
    try:
        mergify_config = rules.get_mergify_config(ctxt)
    except rules.NoRules:  # pragma: no cover
        ctxt.log.info("No need to proceed queue (.mergify.yml is missing)",)
        return
    except rules.InvalidRules as e:  # pragma: no cover
        # Not configured, post status check with the error message
        if any(
            (
                s["event_type"] == "pull_request"
                and s["data"]["action"] in ["opened", "synchronize"]
                for s in ctxt.sources
            )
        ):
            check_api.set_check_run(
                ctxt,
                actions_runner.SUMMARY_NAME,
                "completed",
                "failure",
                output={
                    "title": "The Mergify configuration is invalid",
                    "summary": str(e),
                },
            )
        return

    # Add global and mandatory rules
    mergify_config["pull_request_rules"].rules.extend(
        rules.load_pull_request_rules_schema(MERGIFY_RULE["rules"])
    )

    subscription = sub_utils.get_subscription(
        utils.get_redis_for_cache(), ctxt.client.installation["id"]
    )

    if ctxt.pull["base"]["repo"]["private"] and not subscription["subscription_active"]:
        check_api.set_check_run(
            ctxt,
            actions_runner.SUMMARY_NAME,
            "completed",
            "failure",
            output={
                "title": "Mergify is disabled",
                "summary": subscription["subscription_reason"],
            },
        )
        return

    # CheckRun are attached to head sha, so when user add commits or force push
    # we can't directly get the previous Mergify Summary. So we copy it here, then
    # anything that looks at it in next celery tasks will find it.

    synchronize_data = [
        s["data"]
        for s in ctxt.sources
        if s["event_type"] == "pull_request"
        and s["data"]["action"] == "synchronize"
        and s["data"]["after"] == ctxt.pull["head"]["sha"]
    ]
    if synchronize_data:
        copy_summary_from_previous_head_sha(ctxt, synchronize_data[0]["before"])

    actions_runner.handle(mergify_config["pull_request_rules"], ctxt)
