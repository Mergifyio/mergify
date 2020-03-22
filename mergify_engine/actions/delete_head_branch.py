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

from urllib import parse

import httpx
import voluptuous

from mergify_engine import actions


class DeleteHeadBranchAction(actions.Action):
    only_once = True
    validator = voluptuous.Any(
        {voluptuous.Optional("force", default=False): bool}, None
    )

    def run(self, ctxt, sources, missing_conditions):
        if ctxt.pull_from_fork:
            return ("success", "Pull request come from fork", "")

        if ctxt.pull["state"] == "closed":
            if self.config is None or not self.config["force"]:
                pulls_using_this_branch = list(
                    ctxt.client.items("pulls", json={"base": ctxt.pull["head"]["ref"]})
                )
                if pulls_using_this_branch:
                    return (
                        "success",
                        "Branch `{}` was not deleted "
                        "because it is used by {}".format(
                            ctxt.pull["head"]["ref"],
                            " ".join(
                                "#%d" % p["number"] for p in pulls_using_this_branch
                            ),
                        ),
                        "",
                    )

            ref_to_delete = parse.quote(ctxt.pull["head"]["ref"], safe="")
            try:
                ctxt.client.delete(f"git/refs/heads/{ref_to_delete}")
            except httpx.HTTPClientSideError as e:
                if e.status_code not in [422, 404]:
                    return (
                        "failure",
                        "Unable to delete the head branch",
                        f"GitHub error: [{e.status_code}] `{e.message}`",
                    )
            return (
                "success",
                f"Branch `{ctxt.pull['head']['ref']}` has been deleted",
                "",
            )
        return (
            None,
            f"Branch `{ctxt.pull['head']['ref']}` will be deleted once the pull request is closed",
            "",
        )
