# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
import yaml

from mergify_engine import context
from mergify_engine.tests.functional import base


class TestCloseAction(base.FunctionalTestBase):
    def test_close(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "close",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"close": {"message": "WTF?"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        self.run_engine()

        p.update()
        self.assertEqual("closed", p.state)
        self.assertEqual("WTF?", list(p.get_issue_comments())[-1].body)

    def test_close_template(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "close",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"close": {"message": "Thank you {{author}}"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        self.run_engine()

        p.update()
        self.assertEqual("closed", p.state)
        comments = list(p.get_issue_comments())
        self.assertEqual(f"Thank you {self.u_fork.login}", comments[-1].body)

    def _test_close_template_error(self, msg):
        rules = {
            "pull_request_rules": [
                {
                    "name": "close",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"close": {"message": msg}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        self.run_engine()

        p.update()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})

        assert len(ctxt.pull_engine_check_runs) == 1
        check = ctxt.pull_engine_check_runs[0]
        assert "failure" == check["conclusion"]
        assert "The Mergify configuration is invalid" == check["output"]["title"]
        return check

    def test_close_template_syntax_error(self):
        check = self._test_close_template_error(
            msg="Thank you {{",
        )
        assert (
            """Template syntax error @ data['pull_request_rules'][0]['actions']['close']['message'][line 1]
```
unexpected 'end of template'
```"""
            == check["output"]["summary"]
        )

    def test_close_template_attribute_error(self):
        check = self._test_close_template_error(
            msg="Thank you {{hello}}",
        )
        assert (
            """Template syntax error for dictionary value @ data['pull_request_rules'][0]['actions']['close']['message']
```
Unknown pull request attribute: hello
```"""
            == check["output"]["summary"]
        )
