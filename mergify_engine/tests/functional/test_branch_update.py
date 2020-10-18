# -*- encoding: utf-8 -*-
#
# Copyright © 2020  Mergify SAS
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

from mergify_engine.tests.functional import base


class TestBranchUpdatePublic(base.FunctionalTestBase):
    def test_command_update(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-rebase-on-conflict",
                    "conditions": ["conflict"],
                    "actions": {"comment": {"message": "nothing"}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar"})
        p1, _ = self.create_pr(files={"TESTING2": "foobar"})
        p2, _ = self.create_pr(files={"TESTING3": "foobar"})
        p1.merge()

        self.wait_for("pull_request", {"action": "closed"})

        self.create_message(p2, "@mergifyio update")
        self.run_engine()

        oldsha = p2.head.sha
        p2.update()
        assert p2.commits == 2
        assert oldsha != p2.head.sha

    def test_command_rebase_ok(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-rebase-on-label",
                    "conditions": ["label=rebase"],
                    "actions": {"comment": {"message": "nothing"}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar\n"})
        p1, _ = self.create_pr(files={"TESTING": "foobar\n\n\np1"})
        p2, _ = self.create_pr(files={"TESTING": "p2\n\nfoobar\n"})
        p1.merge()
        self.wait_for("pull_request", {"action": "closed"})

        self.create_message(p2, "@mergifyio rebase")
        self.run_engine()

        self.wait_for("pull_request", {"action": "synchronize"})

        oldsha = p2.head.sha
        p2.merge()
        p2.update()
        assert oldsha != p2.head.sha
        f = p2.base.repo.get_contents("TESTING")
        assert f.decoded_content == b"p2\n\nfoobar\n\n\np1"


# FIXME(sileht): This is not yet possible, due to GH restriction ...
# class TestBranchUpdatePrivate(TestBranchUpdatePublic):
#    REPO_NAME = "functional-testing-repo-private"
#    FORK_PERSONAL_TOKEN = config.ORG_USER_PERSONAL_TOKEN
#    SUBSCRIPTION_ACTIVE = True
