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
import httpx

from mergify_engine.clients import github
from mergify_engine.tasks import github_events
from mergify_engine.worker import app


LOG = daiquiri.getLogger(__name__)


@app.task
def job_refresh(owner, repo, kind, ref=None, action="user"):
    LOG.info("job refresh", kind=kind, ref=ref, gh_owner=owner, gh_repo=repo)

    try:
        client = github.get_client(owner, repo)
    except httpx.HTTPError as e:
        LOG.warning(
            "mergify not installed",
            kind=kind,
            ref=ref,
            gh_owner=owner,
            gh_repo=repo,
            error=str(e),
        )
        return

    if kind == "repo":
        pulls = list(client.items("pulls"))
    elif kind == "branch":
        pulls = list(client.items("pulls", base=ref))
    elif kind == "pull":
        pulls = [client.item(f"pulls/{ref}")]
    else:
        raise RuntimeError("Invalid kind")

    for p in pulls:
        # Mimic the github event format
        data = {
            "action": action,
            "repository": p["base"]["repo"],
            "installation": {"id": client.installation_id},
            "pull_request": p,
            "sender": {"login": "<internal>"},
        }
        github_events.job_filter_and_dispatch.s(
            "refresh", str(uuid.uuid4()), data
        ).apply_async()
