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

import httpx
import voluptuous

from mergify_engine import actions


class LabelAction(actions.Action):
    validator = {
        voluptuous.Required("add", default=[]): [str],
        voluptuous.Required("remove", default=[]): [str],
        voluptuous.Required("remove_all", default=False): bool,
    }

    silent_report = True

    def run(self, ctxt, sources, missing_conditions):
        if self.config["add"]:
            all_label = [l["name"] for l in ctxt.client.items("labels")]
            for label in self.config["add"]:
                if label not in all_label:
                    color = "%06x" % random.randrange(16 ** 6)
                    try:
                        ctxt.client.post("labels", json={"name": label, "color": color})
                    except httpx.HTTPClientSideError:
                        continue

            ctxt.client.post(
                f"issues/{ctxt.pull['number']}/labels",
                json={"labels": self.config["add"]},
            )

        if self.config["remove_all"]:
            ctxt.client.delete(f"issues/{ctxt.pull['number']}/labels")
        elif self.config["remove"]:
            pull_labels = [l["name"] for l in ctxt.pull["labels"]]
            for label in self.config["remove"]:
                if label in pull_labels:
                    label_escaped = parse.quote(label, safe="")
                    try:
                        ctxt.client.delete(
                            f"issues/{ctxt.pull['number']}/labels/{label_escaped}"
                        )
                    except httpx.HTTPClientSideError:
                        continue

        return ("success", "Labels added/removed", "")
