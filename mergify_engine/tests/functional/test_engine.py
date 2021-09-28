# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2021 Mergify SAS
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
import logging
import time
from unittest import mock

import pytest
import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine.clients import github
from mergify_engine.rules import live_resolvers
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestEngineV2Scenario(base.FunctionalTestBase):
    """Mergify engine tests.

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    async def test_no_configuration(self):
        await self.setup_repo()
        p, _ = await self.create_pr()
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        assert (
            "no rules configured, just listening for commands"
            == checks[0]["output"]["title"]
        )

    async def test_empty_configuration(self):
        await self.setup_repo("")
        p, _ = await self.create_pr()
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        assert (
            "no rules configured, just listening for commands"
            == checks[0]["output"]["title"]
        )

    async def test_merge_with_not_merged_attribute(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}", "-merged"],
                    "actions": {"merge": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        for check in await ctxt.pull_check_runs:
            if check["name"] == "Rule: merge on main (merge)":
                assert (
                    "The pull request has been merged automatically"
                    == check["output"]["title"]
                )
                assert (
                    f"The pull request has been merged automatically at *{ctxt.pull['merge_commit_sha']}*"
                    == check["output"]["summary"]
                )
                break
        else:
            pytest.fail("Merge check not found")

    async def test_merge_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on main",
                    "conditions": [f"base={self.main_branch_name}", "label=squash"],
                    "actions": {"merge": {"method": "squash"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr(files={"foo": "bar"})
        p2, _ = await self.create_pr(two_commits=True)
        await self.merge_pull(p1["number"])

        await self.add_label(p2["number"], "squash")

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p2["number"])

        p2 = await self.get_pull(p2["number"])
        assert 2 == p2["commits"]

    async def test_merge_strict_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on main",
                    "conditions": [f"base={self.main_branch_name}", "label=squash"],
                    "actions": {"merge": {"strict": "smart", "method": "squash"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr(files={"foo": "bar"})
        p2, _ = await self.create_pr(two_commits=True)
        await self.merge_pull(p1["number"])

        await self.add_label(p2["number"], "squash")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p2 = await self.get_pull(p2["number"])
        assert 3 == p2["commits"]
        assert p2["merged"] is True

    async def test_merge_strict_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": True, "strict_method": "rebase"}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_main_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        commits2 = await self.get_commits(p2["number"])

        assert 1 == len(commits2)
        assert commits[0]["sha"] != commits2[0]["sha"]
        assert commits[0]["commit"]["message"] == commits2[0]["commit"]["message"]

        # Retry to merge pr2
        p2 = await self.get_pull(p2["number"])
        await self.create_status(p2)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        main_sha = (await self.get_head_commit())["sha"]
        assert previous_main_sha != main_sha

        pulls = await self.get_pulls(params={"base": self.main_branch_name})
        assert 0 == len(pulls)

    async def test_merge_strict_default(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": True}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_main_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        p2 = await self.get_pull(p2["number"])
        commits2 = await self.get_commits(p2["number"])

        # Check main have been merged into the PR
        assert (
            f"Merge branch '{self.main_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            in commits2[-1]["commit"]["message"]
        )

        # Retry to merge pr2
        await self.create_status(p2)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        main_sha = (await self.get_head_commit())["sha"]
        assert previous_main_sha != main_sha

        pulls = await self.get_pulls(params={"base": self.main_branch_name})
        assert 0 == len(pulls)

    async def test_merge_smart_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "strict merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": "smart"}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_main_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2)
        await self.create_review(p2["number"])
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        # Queue with signature
        r = await self.app.get(
            f"/queues/{config.TESTING_ORGANIZATION_ID}",
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )

        assert r.json() == {
            f"{self.RECORD_CONFIG['repository_id']}": {
                self.main_branch_name: [p2["number"]]
            }
        }

        # Queue with token
        r = await self.app.get(
            f"/queues/{config.TESTING_ORGANIZATION_ID}",
            headers={
                "Authorization": f"token {config.ORG_ADMIN_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )

        assert r.json() == {
            f"{self.RECORD_CONFIG['repository_id']}": {
                self.main_branch_name: [p2["number"]]
            }
        }

        p2 = await self.get_pull(p2["number"])
        commits2 = await self.get_commits(p2["number"])

        # Check main have been merged into the PR
        assert (
            f"Merge branch '{self.main_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            in commits2[-1]["commit"]["message"]
        )

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        for check in await ctxt.pull_check_runs:
            if check["name"] == "Rule: strict merge on main (merge)":
                assert (
                    "The pull request is the 1st in the queue to be merged"
                    == check["output"]["title"]
                )
                assert (
                    f"""**Required conditions for merge:**

- [X] `base={self.main_branch_name}`
- [ ] `status-success=continuous-integration/fake-ci`
- [X] `#approved-reviews-by>=1`

**The following pull requests are queued:**
| | Pull request | Priority |
| ---: | :--- | :--- |
| 1 | test_merge_smart_strict: pull request n2 from fork #{p2['number']} | medium |

---
"""
                    in check["output"]["summary"]
                )
                break
        else:
            pytest.fail("Merge check not found")

        # Retry to merge pr2
        await self.create_status(p2)
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        main_sha = (await self.get_head_commit())["sha"]
        assert previous_main_sha != main_sha

        pulls = await self.get_pulls(params={"base": self.main_branch_name})
        assert 0 == len(pulls)

    async def test_merge_failure_smart_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "strict merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart"}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()
        p3, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_main_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2, "continuous-integration/fake-ci", "success")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.create_status(p3, "continuous-integration/fake-ci", "success")
        await self.run_engine()

        p2 = await self.get_pull(p2["number"])
        commits2 = await self.get_commits(p2["number"])
        assert (
            f"Merge branch '{self.main_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            == commits2[-1]["commit"]["message"]
        )

        await self.create_status(p2, "continuous-integration/fake-ci", "failure")
        await self.run_engine()

        # FIXME(sileht): Previous actions tracker was posting a "Rule XXXX (merge)" with
        # neutral status saying the Merge doesn't match anymore, the new one doesn't
        # It's not a big deal as the rule doesn't match anymore anyway.
        await self.wait_for("check_run", {"check_run": {"conclusion": "cancelled"}})

        # Should got to the next PR
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})

        p3 = await self.get_pull(p3["number"])
        commits3 = await self.get_commits(p3["number"])
        assert (
            f"Merge branch '{self.main_branch_name}' into {self.get_full_branch_name('fork/pr3')}"
            == commits3[-1]["commit"]["message"]
        )

        await self.create_status(p3, "continuous-integration/fake-ci", "success")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        main_sha = (await self.get_head_commit())["sha"]
        assert previous_main_sha != main_sha

        pulls = await self.get_pulls(params={"base": self.main_branch_name})
        assert 1 == len(pulls)

    async def test_teams(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "valid teams",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@mergifyio-testing/testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "short teams",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "not exists teams",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@mergifyio-testing/noexists",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "invalid organization",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@another-org/testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, commits = await self.create_pr()
        await self.run_engine()

        client = github.aget_client(p["base"]["user"]["id"])
        installation = context.Installation(
            p["base"]["user"]["id"],
            p["base"]["user"]["login"],
            self.subscription,
            client,
            self.redis_cache,
        )
        repository = context.Repository(installation, p["base"]["repo"])
        ctxt = await context.Context.create(repository, p, [])

        logins = await live_resolvers.teams(
            repository,
            [
                "user",
                "@mergifyio-testing/testing",
            ],
        )

        assert sorted(logins) == sorted(
            [
                "user",
                "mergify-test1",
                "mergify-test3",
            ]
        )

        logins = await live_resolvers.teams(
            repository,
            [
                "user",
                "@testing",
            ],
        )

        assert sorted(logins) == sorted(
            [
                "user",
                "mergify-test1",
                "mergify-test3",
            ]
        )

        with self.assertRaises(live_resolvers.LiveResolutionFailure):
            await live_resolvers.teams(repository, ["@unknown/team"])

        with self.assertRaises(live_resolvers.LiveResolutionFailure):
            await live_resolvers.teams(repository, ["@mergifyio-testing/not-exists"])

        with self.assertRaises(live_resolvers.LiveResolutionFailure):
            await live_resolvers.teams(repository, ["@invalid/team/break-here"])

        summary = [
            c for c in await ctxt.pull_engine_check_runs if c["name"] == "Summary"
        ][0]
        assert summary["output"]["title"] == "2 faulty rules and 2 potential rules"
        for message in (
            "Team `@mergifyio-testing/noexists` does not exist",
            "Team `@another-org/testing` is not part of the organization `mergifyio-testing`",
        ):
            assert message in summary["output"]["summary"]

    async def _test_merge_custom_msg(
        self, header, method="squash", msg=None, commit_msg=None
    ):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": method}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        if msg is None:
            msg = "This is the title\n\nAnd this is the message"
        p, _ = await self.create_pr(message=f"It fixes it\n\n## {header}{msg}")
        await self.create_status(p)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        commit = (await self.get_head_commit())["commit"]
        if commit_msg is None:
            commit_msg = msg
        assert commit_msg == commit["message"]

    async def test_merge_custom_msg(self):
        return await self._test_merge_custom_msg("Commit Message:\n")

    async def test_merge_custom_msg_case(self):
        return await self._test_merge_custom_msg("Commit message\n")

    async def test_merge_custom_msg_rn(self):
        return await self._test_merge_custom_msg("Commit Message\r\n")

    async def test_merge_custom_msg_merge(self):
        return await self._test_merge_custom_msg("Commit Message:\n", "merge")

    async def test_merge_custom_msg_template(self):
        return await self._test_merge_custom_msg(
            "Commit Message:\n",
            "merge",
            msg="{{title}}\n\nThanks to {{author}}",
            commit_msg="test_merge_custom_msg_template: pull request n1 from fork\n\nThanks to mergify-test2",
        )

    async def test_merge_invalid_custom_msg(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": "merge"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        msg = "This is the title\n\nAnd this is the message {{invalid}}"
        p, _ = await self.create_pr(message=f"It fixes it\n\n## Commit Message\n{msg}")
        await self.create_status(p)

        await self.run_engine()

        assert not await self.is_pull_merged(p["number"])

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "completed" == checks[0]["status"]
        assert checks[0]["conclusion"] == "action_required"
        assert (
            "Unknown pull request attribute: invalid" == checks[0]["output"]["summary"]
        )
        assert "Invalid commit message" == checks[0]["output"]["title"]

        # Edit and fixes the typo
        await self.edit_pull(
            p["number"], body="It fixes it\n\n## Commit Message\n\nHere it is valid now"
        )
        await self.wait_for("pull_request", {"action": "edited"})

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for(
            "check_run",
            {"check_run": {"conclusion": "success", "status": "completed"}},
        )

        # delete check run cache
        del ctxt._cache["pull_check_runs"]
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "completed" == checks[0]["status"]
        assert checks[0]["conclusion"] == "success"

        assert await self.is_pull_merged(p["number"])

    async def test_merge_custom_msg_title_body(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {
                        "merge": {"method": "merge", "commit_message": "title+body"}
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        msg = "It fixes it"
        p, _ = await self.create_pr(message=msg)
        await self.create_status(p)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        commit = (await self.get_head_commit())["commit"]
        assert (
            f"test_merge_custom_msg_title_body: pull request n1 from fork (#{p['number']})\n\n{msg}"
            == commit["message"]
        )

    async def test_merge_and_closes_issues(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": "merge"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        i = await self.create_issue(
            title="Such a bug", body="I can't explain, but don't work"
        )
        p, commits = await self.create_pr(
            message=f"It fixes it\n\nCloses #{i['number']}"
        )
        await self.create_status(p)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        pulls = await self.get_pulls(
            params={"state": "all", "base": self.main_branch_name}
        )
        assert 1 == len(pulls)
        assert p["number"] == pulls[0]["number"]
        assert "closed" == pulls[0]["state"]

        issue = await self.client_admin.item(f"{self.url_origin}/issues/{i['number']}")
        assert "closed" == issue["state"]

    async def test_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p2, commits = await self.create_pr()
        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p2["number"])

    async def test_merge_branch_protection_ci(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "merge": {"method": "rebase"},
                        "comment": {"message": "yo"},
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        protection = {
            "required_status_checks": {
                "strict": False,
                "contexts": [
                    "continuous-integration/fake-ci",
                    "neutral-ci",
                    "skipped-ci",
                ],
            },
            "required_pull_request_reviews": {
                "require_code_owner_reviews": True,
                "required_approving_review_count": 1,
            },
            "required_linear_history": True,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.main_branch_name, protection)

        p, _ = await self.create_pr()

        await self.run_engine()

        await self.wait_for(
            "check_run",
            {
                "check_run": {
                    "conclusion": "success",
                    "status": "completed",
                    "name": "Summary",
                }
            },
        )

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert checks == []
        summary = [
            c for c in await ctxt.pull_engine_check_runs if c["name"] == "Summary"
        ][0]
        assert f"""### Rule: merge (merge)
- [X] `base={self.main_branch_name}`
- [ ] any of: [🛡 GitHub branch protection]
  - [ ] `check-success=continuous-integration/fake-ci`
  - [ ] `check-neutral=continuous-integration/fake-ci`
  - [ ] `check-skipped=continuous-integration/fake-ci`
- [ ] any of: [🛡 GitHub branch protection]
  - [ ] `check-success=neutral-ci`
  - [ ] `check-neutral=neutral-ci`
  - [ ] `check-skipped=neutral-ci`
- [ ] any of: [🛡 GitHub branch protection]
  - [ ] `check-success=skipped-ci`
  - [ ] `check-neutral=skipped-ci`
  - [ ] `check-skipped=skipped-ci`
- [X] `linear-history` [🛡 GitHub branch protection]
- [ ] `#approved-reviews-by>=1` [🛡 GitHub branch protection]
- [X] `#changes-requested-reviews-by=0` [🛡 GitHub branch protection]
- [X] `#review-requested=0` [🛡 GitHub branch protection]

### Rule: merge (comment)
- [X] `base={self.main_branch_name}`
""" == "\n".join(
            summary["output"]["summary"].split("\n")[:22]
        )

        await self.create_status(p)
        await self.create_review(p["number"])
        await check_api.set_check_run(
            ctxt,
            "neutral-ci",
            check_api.Result(check_api.Conclusion.NEUTRAL, title="bla", summary=""),
        )
        await check_api.set_check_run(
            ctxt,
            "skipped-ci",
            check_api.Result(
                check_api.Conclusion.SKIPPED, title="bla-skipped", summary=""
            ),
        )

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

    async def test_merge_branch_protection_strict_ci(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {"strict": True}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        protection = {
            "required_status_checks": {
                "strict": True,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.main_branch_name, protection)

        p, _ = await self.create_pr()

        p2, _ = await self.create_pr()
        await self.create_status(p2)
        await self.merge_pull(p2["number"])

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.create_status(p)
        await self.run_engine()

        await self.wait_for(
            "check_run",
            {
                "check_run": {
                    "conclusion": None,
                    "status": "in_progress",
                    "name": "Rule: merge (merge)",
                }
            },
        )

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert checks[0]["conclusion"] is None
        assert "The pull request will be merged soon" == checks[0]["output"]["title"]

        # Ensure it have been  rebased
        head_sha = p["head"]["sha"]
        p = await self.get_pull(p["number"])
        assert p["head"]["sha"] != head_sha

        # After rebase CI fail and PR is removed from queue
        await self.create_status(p, state="failure")
        await self.run_engine()

        await self.wait_for(
            "check_run",
            {
                "check_run": {
                    "conclusion": "success",
                    "status": "completed",
                    "name": "Summary",
                }
            },
        )
        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "cancelled" == checks[0]["conclusion"]
        assert "The rule doesn't match anymore" == checks[0]["output"]["title"]
        assert p["merged"] is False

        # CI is back, check PR is merged
        await self.create_status(p)
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "success" == checks[0]["conclusion"]
        assert (
            "The pull request has been merged automatically"
            == checks[0]["output"]["title"]
        )
        assert p["merged"] is True

    async def test_merge_branch_protection_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        protection = {
            "required_status_checks": {
                "strict": True,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        await self.merge_pull(p1["number"])

        await self.branch_protection_protect(self.main_branch_name, protection)

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_status(p2)

        await self.run_engine()
        await self.wait_for("check_run", {"check_run": {"conclusion": "failure"}})

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "failure" == checks[0]["conclusion"]
        assert (
            "Branch protection setting 'strict' conflicts with Mergify configuration"
            == checks[0]["output"]["title"]
        )

    async def test_refresh_via_check_suite_rerequest(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()

        await self.run_engine()

        ctxt = await self.repository_ctxt.get_pull_request_context(p["number"], p)
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check_suite_id = checks[0]["check_suite"]["id"]

        check_suite = (
            await self.installation_ctxt.client.get(
                f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}",
                api_version="antiope",
            )
        ).json()
        assert check_suite["status"] == "completed"

        # Click on rerequest btn
        await self.installation_ctxt.client.post(
            f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}/rerequest",
            api_version="antiope",
        )
        await self.wait_for("check_suite", {"action": "rerequested"})

        check_suite = (
            await self.installation_ctxt.client.get(
                f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}",
                api_version="antiope",
            )
        ).json()
        assert check_suite["status"] == "queued"

        await self.run_engine()
        check_suite = (
            await self.installation_ctxt.client.get(
                f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}",
                api_version="antiope",
            )
        ).json()
        assert check_suite["status"] == "completed"

    async def test_refresh_api(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        resp = await self.app.post(
            f"/refresh/{p1['base']['repo']['full_name']}/pull/{p1['number']}",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

        resp = await self.app.post(
            f"/refresh/{p2['base']['repo']['full_name']}/pull/{p2['number']}",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

        resp = await self.app.post(
            f"/refresh/{p1['base']['repo']['full_name']}/branch/main",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

        resp = await self.app.post(
            f"/refresh/{p1['base']['repo']['full_name']}",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

    async def test_command_refresh(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p, commits = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(
            self.repository_ctxt,
            p,
        )
        await (
            ctxt.set_summary_check(
                check_api.Result(
                    check_api.Conclusion.SUCCESS,
                    title="whatever",
                    summary="erased",
                )
            )
        )

        checks = await ctxt.pull_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        completed_at = checks[0]["completed_at"]

        await self.create_comment(p["number"], "@mergifyio refresh")

        await self.run_engine()

        del ctxt._cache["pull_check_runs"]
        checks = await ctxt.pull_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        assert completed_at != checks[0]["completed_at"]

        comments = await self.get_issue_comments(p["number"])
        assert (
            "**Command `refresh`: success**\n> **Pull request refreshed**\n> \n"
            == comments[-1]["body"]
        )

    async def test_marketplace_event(self):
        with mock.patch(
            "mergify_engine.subscription.Subscription.get_subscription"
        ) as get_sub:
            get_sub.return_value = self.subscription
            r = await self.app.post(
                "/marketplace",
                headers={
                    "X-Hub-Signature": "sha1=whatever",
                    "Content-type": "application/json",
                },
                json={
                    "sender": {"login": "jd"},
                    "marketplace_purchase": {
                        "account": {
                            "id": 12345,
                            "login": "mergifyio-testing",
                            "type": "Organization",
                        }
                    },
                },
            )
        assert r.content == b"Event queued"
        assert r.status_code == 202

    async def test_refresh_on_conflict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment-on-conflict",
                    "conditions": ["conflict"],
                    "actions": {"comment": {"message": "It conflict!"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar"})
        p1, _ = await self.create_pr(files={"TESTING": "p1"})
        p2, _ = await self.create_pr(files={"TESTING": "p2"})
        await self.merge_pull(p1["number"])

        await self.run_engine()

        # Wait a bit than Github refresh the mergeable_state before running the
        # engine
        if base.RECORD:
            time.sleep(10)

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for(
            "issue_comment",
            {"action": "created", "comment": {"body": "It conflict!"}},
        )

    async def test_requested_reviews(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "user",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "review-requested=mergify-test3",
                    ],
                    "actions": {"comment": {"message": "review-requested user"}},
                },
                {
                    "name": "team",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "review-requested=@testing",
                    ],
                    "actions": {"comment": {"message": "review-requested team"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        await self.create_review_request(p1["number"], reviewers=["mergify-test3"])
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        # FIXME(sileht): This doesn't work anymore MRGFY-227
        # p2, _ = await self.create_pr()
        # p2.create_review_request(team_reviewers=[team.slug])
        # await self.wait_for("pull_request", {"action": "review_requested"})
        # await self.run_engine()
        # await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(p1["number"])
        assert "review-requested user" == comments[0]["body"]

        # assert "review-requested team" == list(p2.get_issue_comments())[0].body

    async def test_truncated_check_output(self):
        # not used anyhow
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, commits = await self.create_pr()
        await self.run_engine()
        pull = await context.Context.create(self.repository_ctxt, pr, [])
        check = await check_api.set_check_run(
            pull,
            "Test",
            check_api.Result(
                check_api.Conclusion.SUCCESS, title="bla", summary="a" * 70000
            ),
        )
        assert check["output"]["summary"] == ("a" * 65532 + "…")

    async def test_pull_request_init_summary(self):
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        await self.setup_repo(yaml.dump(rules))

        # Run the engine once, to initialiaze the config location cache
        await self.create_pr()
        await self.run_engine()

        # Check initial summary is submitted
        p, _ = await self.create_pr()
        ctxt = await self.repository_ctxt.get_pull_request_context(p["number"], p)
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["output"]["title"] == "Your rules are under evaluation"
        assert (
            checks[0]["output"]["summary"]
            == "Be patient, the page will be updated soon."
        )

    async def test_pull_refreshed_after_config_change(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "default",
                    "conditions": ["base=other"],
                    "actions": {"comment": {"message": "it works"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(files={"foo": "bar"})
        await self.run_engine()

        rules["pull_request_rules"][0]["conditions"][
            0
        ] = f"base={self.main_branch_name}"
        p_config, _ = await self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        await self.merge_pull(p_config["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {})

        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(p["number"])
        assert "it works" == comments[-1]["body"]

    async def test_check_run_api(self):
        await self.setup_repo()
        p, _ = await self.create_pr()
        ctxt = await context.Context.create(self.repository_ctxt, p, [])

        await check_api.set_check_run(
            ctxt,
            "Test",
            check_api.Result(
                check_api.Conclusion.PENDING, title="PENDING", summary="PENDING"
            ),
        )
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["status"] == "in_progress"
        assert checks[0]["conclusion"] is None
        assert checks[0]["completed_at"] is None
        assert checks[0]["output"]["title"] == "PENDING"
        assert checks[0]["output"]["summary"] == "PENDING"

        await check_api.set_check_run(
            ctxt,
            "Test",
            check_api.Result(
                check_api.Conclusion.CANCELLED, title="CANCELLED", summary="CANCELLED"
            ),
        )
        ctxt._cache = {}
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["status"] == "completed"
        assert checks[0]["conclusion"] == "cancelled"
        assert checks[0]["completed_at"] is not None
        assert checks[0]["output"]["title"] == "CANCELLED"
        assert checks[0]["output"]["summary"] == "CANCELLED"

        await check_api.set_check_run(
            ctxt,
            "Test",
            check_api.Result(
                check_api.Conclusion.PENDING, title="PENDING", summary="PENDING"
            ),
        )
        ctxt._cache = {}
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["status"] == "in_progress"
        assert checks[0]["conclusion"] is None
        assert checks[0]["completed_at"] is None
        assert checks[0]["output"]["title"] == "PENDING"
        assert checks[0]["output"]["summary"] == "PENDING"

    async def test_get_repository_by_id(self):
        repo = await self.installation_ctxt.get_repository_by_id(
            self.RECORD_CONFIG["repository_id"]
        )
        assert repo.repo["name"] == self.RECORD_CONFIG["repository_name"]
        assert repo.repo["name"] == self.repository_ctxt.repo["name"]


class TestEngineWithSubscription(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_merge_strict_rebase_with_user(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {
                        "merge": {
                            "strict": True,
                            "strict_method": "rebase",
                            "update_bot_account": "mergify-test1",
                        }
                    },
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        commits2 = await self.get_commits(p2["number"])
        events2 = [
            e
            async for e in self.client_admin.items(
                f"{self.url_origin}/issues/{p2['number']}/events"
            )
        ]
        assert 1 == len(commits2)
        assert commits[0]["sha"] != commits2[0]["sha"]
        assert commits[0]["commit"]["message"] == commits2[0]["commit"]["message"]
        assert events2[0]["actor"]["login"] == "mergify-test1"

    async def test_merge_strict_rebase_with_invalid_user(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {
                        "merge": {
                            "strict": True,
                            "strict_method": "rebase",
                            "update_bot_account": "not-exists",
                        }
                    },
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for(
            "check_run", {"check_run": {"conclusion": "action_required"}}
        )

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: smart strict merge on main (merge)"
        ]
        assert (
            checks[0]["output"]["title"]
            == "`not-exists` account used as `update_bot_account` does not exists"
        )
