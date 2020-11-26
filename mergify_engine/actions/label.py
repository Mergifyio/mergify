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
import typing
from urllib import parse

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine.clients import http


class LabelAction(actions.Action):
    validator = {
        voluptuous.Required("add", default=[]): [str],
        voluptuous.Required("remove", default=[]): [str],
        voluptuous.Required("remove_all", default=False): bool,
        voluptuous.Required("add_when_unmatch", default=[]): [str],
        voluptuous.Required("remove_when_unmatch", default=[]): [str],
    }

    silent_report = True

    def run(self, ctxt: context.Context, rule: rules.EvaluatedRule) -> check_api.Result:
        if self.config["add"]:
            self._add_labels(ctxt, self.config["add"])
        if self.config["remove_all"]:
            ctxt.client.delete(f"{ctxt.base_url}/issues/{ctxt.pull['number']}/labels")
        elif self.config["remove"]:
            self._remove_labels(ctxt, self.config["remove"])

        return check_api.Result(
            check_api.Conclusion.SUCCESS, "Labels added/removed", ""
        )

    def cancel(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        if self.config["remove_when_unmatch"]:
            self._remove_labels(ctxt, self.config["remove_when_unmatch"])

        if self.config["add_when_unmatch"]:
            self._add_labels(ctxt, self.config["add_when_unmatch"])

        return check_api.Result(
            check_api.Conclusion.CANCELLED, "Labels added/removed", ""
        )

    def _add_labels(
        self, ctxt: context.Context, labels_to_add: typing.List[str]
    ) -> None:
        all_label = [
            label["name"] for label in ctxt.client.items(f"{ctxt.base_url}/labels")
        ]
        for label in labels_to_add:
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
            json={"labels": labels_to_add},
        )

    def _remove_labels(
        self, ctxt: context.Context, labels_to_remove: typing.List[str]
    ) -> None:
        pull_labels = [label["name"] for label in ctxt.pull["labels"]]
        for label in labels_to_remove:
            if label in pull_labels:
                label_escaped = parse.quote(label, safe="")
                try:
                    ctxt.client.delete(
                        f"{ctxt.base_url}/issues/{ctxt.pull['number']}/labels/{label_escaped}"
                    )
                except http.HTTPClientSideError:
                    continue
