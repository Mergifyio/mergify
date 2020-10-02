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

import random
from urllib import parse

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine.clients import http


class LabelAction(actions.Action):
    validator = {
        voluptuous.Required("add", default=[]): [str],
        voluptuous.Required("remove", default=[]): [str],
        voluptuous.Required("remove_all", default=False): bool,
    }

    silent_report = True

    def run(self, ctxt, rule, missing_conditions) -> check_api.Result:
        if self.config["add"]:
            all_label = [
                label["name"] for label in ctxt.client.items(f"{ctxt.base_url}/labels")
            ]
            for label in self.config["add"]:
                if label not in all_label:
                    color = "%06x" % random.randrange(16 ** 6)
                    try:
                        ctxt.client.post(
                            f"{ctxt.base_url}/labels",
                            json={"name": label, "color": color},
                        )
                    except http.HTTPClientSideError:
                        continue

            ctxt.client.post(
                f"{ctxt.base_url}/issues/{ctxt.pull['number']}/labels",
                json={"labels": self.config["add"]},
            )

        if self.config["remove_all"]:
            ctxt.client.delete(f"{ctxt.base_url}/issues/{ctxt.pull['number']}/labels")
        elif self.config["remove"]:
            pull_labels = [label["name"] for label in ctxt.pull["labels"]]
            for label in self.config["remove"]:
                if label in pull_labels:
                    label_escaped = parse.quote(label, safe="")
                    try:
                        ctxt.client.delete(
                            f"{ctxt.base_url}/issues/{ctxt.pull['number']}/labels/{label_escaped}"
                        )
                    except http.HTTPClientSideError:
                        continue

        return check_api.Result(
            check_api.Conclusion.SUCCESS, "Labels added/removed", ""
        )
