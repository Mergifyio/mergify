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

import uuid

import daiquiri

import github


from mergify_engine import config
from mergify_engine import utils
from mergify_engine.tasks import github_events
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


@app.task
def job_refresh(owner, repo, kind, ref=None):
    LOG.info("job refresh", kind=kind, ref=ref, gh_owner=owner, gh_repo=repo)

    integration = github.GithubIntegration(config.INTEGRATION_ID, config.PRIVATE_KEY)
    try:
        installation_id = utils.get_installation_id(integration, owner, repo)
    except github.GithubException as e:
        LOG.warning(
            "mergify not installed",
            kind=kind,
            ref=ref,
            gh_owner=owner,
            gh_repo=repo,
            error=str(e),
        )
        return

    token = integration.get_access_token(installation_id).token
    g = github.Github(token, base_url="https://api.%s" % config.GITHUB_DOMAIN)
    r = g.get_repo("%s/%s" % (owner, repo))

    if kind == "repo":
        pulls = r.get_pulls()
    elif kind == "branch":
        pulls = r.get_pulls(base=ref)
    elif kind == "pull":
        pulls = [r.get_pull(ref)]
    else:
        raise RuntimeError("Invalid kind")

    for p in pulls:
        # Mimic the github event format
        data = {
            "repository": r.raw_data,
            "installation": {"id": installation_id},
            "pull_request": p.raw_data,
            "sender": {"login": "<internal>"},
        }
        github_events.job_filter_and_dispatch.s(
            "refresh", str(uuid.uuid4()), data
        ).apply_async()
