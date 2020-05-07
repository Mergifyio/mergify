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

from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import utils
from mergify_engine.clients import github_app
from mergify_engine.tasks import app


LOG = logs.getLogger(__name__)


@app.task
def job_marketplace(event_type, event_id, data):

    owner = data["marketplace_purchase"]["account"]["login"]
    account_type = data["marketplace_purchase"]["account"]["type"]
    try:
        installation = github_app.get_client().get_installation(
            owner, account_type=account_type
        )
    except exceptions.MergifyNotInstalled:
        return

    r = utils.get_redis_for_cache()
    r.delete("subscription-cache-%s" % installation["id"])

    LOG.info(
        "Marketplace event",
        event_type=event_type,
        event_id=event_id,
        install_id=installation["id"],
        sender=data["sender"]["login"],
        gh_owner=owner,
    )
