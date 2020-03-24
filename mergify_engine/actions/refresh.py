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

from mergify_engine import actions
from mergify_engine.tasks import github_events


class RefreshAction(actions.Action):
    is_command = True
    is_action = False
    validator = {}

    def run(self, ctxt, sources, missing_conditions):
        data = {
            "action": "user",
            "repository": ctxt.pull["base"]["repo"],
            "installation": {"id": ctxt.client.installation["id"]},
            "pull_request": ctxt.pull,
            "sender": {"login": "<internal>"},
        }
        github_events.job_filter_and_dispatch.s(
            "refresh", str(uuid.uuid4()), data
        ).apply_async()
