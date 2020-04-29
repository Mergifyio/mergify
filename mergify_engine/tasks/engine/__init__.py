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

import httpx

from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import worker
from mergify_engine.clients import github
from mergify_engine.tasks import app


LOG = logs.getLogger(__name__)


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


def filter_event_data(event_type, data):
    slim_data = {"sender": data["sender"]}

    # For pull_request opened/synchronise/closed
    # and refresh event
    for attr in ("action", "after", "before"):
        if attr in data:
            slim_data[attr] = data[attr]

    # For commands runner
    if event_type == "issue_comment":
        slim_data["comment"] = data["comment"]
    return slim_data


# NOTE(sileht): We keep this as task one hour to ensure all celery tasks are done and then we
# can remove @app.task
@app.task
def run(event_type, data):
    """ Extract the pull request from the event and run the engine with it """
    installation_id = data["installation"]["id"]
    owner = data["repository"]["owner"]["login"]
    repo = data["repository"]["name"]

    try:
        installation = github.get_installation(owner, repo, installation_id)
    except exceptions.MergifyNotInstalled:
        return

    with github.get_client(owner, repo, installation) as client:
        pull = get_github_pull_from_event(client, event_type, data)

        if not pull:  # pragma: no cover
            LOG.info(
                "No pull request found in the event %s, ignoring",
                event_type,
                gh_owner=owner,
                gh_repo=repo,
            )
            return

        slim_data = filter_event_data(event_type, data)
        worker.push(
            installation["id"], owner, repo, pull["number"], event_type, slim_data
        )
