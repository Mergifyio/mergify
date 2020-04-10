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


from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine.clients import github
from mergify_engine.engine import commands_runner
from mergify_engine.tasks import app


@app.task
def run_command_async(
    installation_id, pull_request_raw, sources, comment, user, rerun=False
):
    owner = pull_request_raw["base"]["user"]["login"]
    repo = pull_request_raw["base"]["repo"]["name"]

    try:
        installation = github.get_installation(owner, repo, installation_id)
    except exceptions.MergifyNotInstalled:
        return

    with github.get_client(owner, repo, installation) as client:
        ctxt = context.Context(client, pull_request_raw, sources)
        return commands_runner.handle(ctxt, comment, user, rerun)
