# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2021 Mergify SAS
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
import operator

from freezegun import freeze_time
import pytest
import yaml

from mergify_engine import constants
from mergify_engine import context
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestAttributes(base.FunctionalTestBase):
    async def test_jit_schedule_on_queue_rules(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": ["schedule: MON-FRI 08:00-17:00"],
                    "allow_inplace_speculative_checks": False,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "fast queue",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"queue": {"name": "default"}},
                }
            ],
        }
        with freeze_time("2021-09-22T08:00:02", tick=True):
            await self.setup_repo(yaml.dump(rules))
            pr, _ = await self.create_pr()
            pr_force_rebase, _ = await self.create_pr()
            await self.merge_pull(pr_force_rebase["number"])
            await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
            await self.run_engine()
            await self.wait_for("pull_request", {"action": "opened"})
            await self.run_engine()
            pr = await self.get_pull(pr["number"])
            assert pr["merged"]

    async def test_time(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["current-time>=12:00"],
                    "actions": {"comment": {"message": "it's time"}},
                }
            ]
        }
        with freeze_time("2021-05-30T10:00:00", tick=True):
            await self.setup_repo(yaml.dump(rules))
            pr, _ = await self.create_pr()
            comments = await self.get_issue_comments(pr["number"])
            assert len(comments) == 0
            await self.run_engine()
            comments = await self.get_issue_comments(pr["number"])
            assert len(comments) == 0

            assert await self.redis_cache.zcard("delayed-refresh") == 1

        with freeze_time("2021-05-30T14:00:00", tick=True):
            await self.run_engine()
            await self.wait_for("issue_comment", {"action": "created"})
            comments = await self.get_issue_comments(pr["number"])
            self.assertEqual("it's time", comments[-1]["body"])

    async def test_disabled(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "disabled": {"reason": "code freeze"},
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "-closed",
                        "label!=foo",
                    ],
                    "actions": {"close": {}},
                },
                {
                    "name": "nothing",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "closed",
                        "label=foo",
                    ],
                    "actions": {},
                },
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        pr, _ = await self.create_pr()
        ctxt = await context.Context.create(self.repository_ctxt, pr)
        await self.run_engine()
        assert (await self.get_pull(pr["number"]))["state"] == "open"
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        expected = (
            "### Rule: ~~merge (close)~~\n:no_entry_sign: **Disabled: code freeze**\n"
        )
        assert expected == summary["output"]["summary"][: len(expected)]

    async def test_schedule(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["schedule=MON-FRI 12:00-15:00"],
                    "actions": {"comment": {"message": "it's time"}},
                }
            ]
        }
        with freeze_time("2021-05-30T10:00:00", tick=True):
            await self.setup_repo(yaml.dump(rules))
            pr, _ = await self.create_pr()
            comments = await self.get_issue_comments(pr["number"])
            assert len(comments) == 0
            await self.run_engine()
            comments = await self.get_issue_comments(pr["number"])
            assert len(comments) == 0

            assert await self.redis_cache.zcard("delayed-refresh") == 1

        with freeze_time("2021-06-02T14:00:00", tick=True):
            await self.run_engine()
            await self.wait_for("issue_comment", {"action": "created"})
            comments = await self.get_issue_comments(pr["number"])
            self.assertEqual("it's time", comments[-1]["body"])

    async def test_updated_relative_not_match(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["created-at<9999 days ago"],
                    "actions": {"comment": {"message": "it's time"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr()
        comments = await self.get_issue_comments(pr["number"])
        assert len(comments) == 0
        await self.run_engine()
        comments = await self.get_issue_comments(pr["number"])
        assert len(comments) == 0

        assert await self.redis_cache.zcard("delayed-refresh") == 1

    async def test_commits_behind_conditions_pr_open_before(self):
        await self.setup_repo()
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
        pr_force_rebase, _ = await self.create_pr(two_commits=True)
        await self.merge_pull(pr_force_rebase["number"])
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p)
        assert await ctxt.is_behind
        assert len(await ctxt.commits_behind) == 3
        assert ctxt.pull["mergeable_state"] == "behind"

        await self.client_admin.put(
            f"{self.repository_ctxt.base_url}/pulls/{p['number']}/update-branch",
            api_version="lydian",
            json={"expected_head_sha": p["head"]["sha"]},
        )
        await self.wait_for("pull_request", {"action": "synchronize"})

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p)
        assert ctxt.pull["mergeable_state"] != "behind"
        assert not await ctxt.is_behind
        assert len(await ctxt.commits_behind) == 0

    async def test_commits_behind_conditions_pr_open_after(self):
        await self.setup_repo()

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

        pr_force_rebase, _ = await self.create_pr(two_commits=True)
        await self.merge_pull(pr_force_rebase["number"])
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        await self.git("reset", "--hard", "HEAD^^")
        p, _ = await self.create_pr(git_tree_ready=True)
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p)
        assert ctxt.pull["mergeable_state"] == "behind"
        assert await ctxt.is_behind
        assert len(await ctxt.commits_behind) == 3

        await self.client_admin.put(
            f"{self.repository_ctxt.base_url}/pulls/{p['number']}/update-branch",
            api_version="lydian",
            json={"expected_head_sha": p["head"]["sha"]},
        )
        await self.wait_for("pull_request", {"action": "synchronize"})

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p)
        assert ctxt.pull["mergeable_state"] != "behind"
        assert not await ctxt.is_behind
        assert len(await ctxt.commits_behind) == 0

    async def test_updated_relative_match(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["created-at>=9999 days ago"],
                    "actions": {"comment": {"message": "it's time"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr()
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("it's time", comments[-1]["body"])

    async def test_draft(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["draft"],
                    "actions": {"comment": {"message": "draft pr"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        protection = {
            "required_status_checks": {
                "strict": False,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }
        await self.branch_protection_protect(self.main_branch_name, protection)

        pr, _ = await self.create_pr()
        ctxt = await context.Context.create(self.repository_ctxt, pr)
        assert not await ctxt.pull_request.draft

        pr, _ = await self.create_pr(draft=True)

        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        ctxt = await context.Context.create(
            self.repository_ctxt,
            {
                "number": pr["number"],
                "base": {
                    "user": {"login": pr["base"]["user"]["login"]},
                    "repo": {
                        "name": pr["base"]["repo"]["name"],
                    },
                },
            },
        )
        assert await ctxt.pull_request.draft

        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("draft pr", comments[-1]["body"])

        # Test underscore/dash attributes
        assert await ctxt.pull_request.review_requested == []

        with pytest.raises(AttributeError):
            assert await ctxt.pull_request.foobar

        # Test items
        assert list(ctxt.pull_request) == list(
            context.PullRequest.ATTRIBUTES | context.PullRequest.LIST_ATTRIBUTES
        )
        assert await ctxt.pull_request.items() == {
            "number": pr["number"],
            "closed": False,
            "locked": False,
            "assignee": [],
            "approved-reviews-by": [],
            "files": ["test2"],
            "check-neutral": [],
            "status-neutral": [],
            "commented-reviews-by": [],
            "commits-unverified": ["test_draft: pull request n2 from fork"],
            "milestone": "",
            "label": [],
            "linear-history": True,
            "body": "test_draft: pull request n2 from fork",
            "body-raw": "test_draft: pull request n2 from fork",
            "base": self.main_branch_name,
            "review-requested": [],
            "check-success": ["Summary"],
            "status-success": ["Summary"],
            "changes-requested-reviews-by": [],
            "merged": False,
            "commits": ["test_draft: pull request n2 from fork"],
            "commits-behind": [],
            "head": self.get_full_branch_name("fork/pr2"),
            "author": "mergify-test2",
            "dismissed-reviews-by": [],
            "merged-by": "",
            "queue-position": -1,
            "check-failure": [],
            "status-failure": [],
            "title": "test_draft: pull request n2 from fork",
            "conflict": False,
            "check-pending": ["continuous-integration/fake-ci"],
            "check-stale": [],
            "check-success-or-neutral": ["Summary"],
            "check-skipped": [],
        }

    async def test_and_or(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": [
                        {
                            "or": [
                                {
                                    "and": [
                                        f"base={self.main_branch_name}",
                                        "closed",
                                        "label=foo",
                                    ]
                                },
                                "merged",
                            ]
                        },
                    ],
                    "actions": {"comment": {"message": "and or pr"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        pr, _ = await self.create_pr()
        await self.add_label(pr["number"], "foo")
        await self.edit_pull(pr["number"], state="closed")

        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("and or pr", comments[-1]["body"])

    async def test_one_commit_unverified(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "commits-unverified",
                    "conditions": ["#commits-unverified=1"],
                    "actions": {"comment": {"message": "commits unverified"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr(two_commits=False)
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("commits unverified", comments[-1]["body"])

    async def test_two_commits_unverified(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "commits-unverified",
                    "conditions": ["#commits-unverified=2"],
                    "actions": {"comment": {"message": "commits unverified"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr(two_commits=True)
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("commits unverified", comments[-1]["body"])
        self.assertEqual("commits unverified", comments[0]["body"])

    async def test_one_commit_unverified_message(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "commits-unverified",
                    "conditions": [
                        'commits-unverified="test_one_commit_unverified_message: pull request n1 from fork"'
                    ],
                    "actions": {"comment": {"message": "commits unverified"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr(two_commits=True)
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("commits unverified", comments[-1]["body"])

    async def test_one_commit_unverified_message_wrong(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "commits-unverified",
                    "conditions": ['commits-unverified="foo"'],
                    "actions": {"comment": {"message": "foo test"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr(two_commits=True)
        await self.run_engine()
        comments = await self.get_issue_comments(pr["number"])
        assert len(comments) == 0

    async def test_one_commit_verified(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "commits-unverified",
                    "conditions": ["#commits-unverified=0"],
                    "actions": {"comment": {"message": "commits verified"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr(verified=True)
        ctxt = await context.Context.create(self.repository_ctxt, pr)
        assert len(await ctxt.commits) == 1
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("commits verified", comments[-1]["body"])

    async def test_two_commits_verified(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "commits-unverified",
                    "conditions": ["#commits-unverified=0"],
                    "actions": {"comment": {"message": "commits verified"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, _ = await self.create_pr(verified=True, two_commits=True)
        ctxt = await context.Context.create(self.repository_ctxt, pr)
        assert len(await ctxt.commits) == 2
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("commits verified", comments[-1]["body"])


class TestAttributesWithSub(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_depends_on(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=automerge",
                    ],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        pr1, _ = await self.create_pr()
        pr2, _ = await self.create_pr()
        await self.merge_pull(pr2["number"])

        body = f"Awesome body\nDepends-On: #{pr1['number']}\ndepends-on: #{pr2['number']}\ndepends-On: #9999999"
        pr, _ = await self.create_pr(message=body)
        await self.add_label(pr["number"], "automerge")
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, pr)
        assert ctxt.get_depends_on() == [pr1["number"], pr2["number"], 9999999]
        assert await ctxt._get_consolidated_data("depends-on") == [f"#{pr2['number']}"]

        repo_url = ctxt.pull["base"]["repo"]["html_url"]
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        expected = f"""### Rule: merge (merge)
- [X] `base={self.main_branch_name}`
- [X] `label=automerge`
- [ ] `depends-on=#{pr1['number']}` [⛓️ **test_depends_on: pull request n1 from fork** ([#{pr1['number']}]({repo_url}/pull/{pr1['number']}))]
- [X] `depends-on=#{pr2['number']}` [⛓️ **test_depends_on: pull request n2 from fork** ([#{pr2['number']}]({repo_url}/pull/{pr2['number']}))]
- [ ] `depends-on=#9999999` [⛓️ ⚠️ *pull request not found* (#9999999)]
"""
        assert expected in summary["output"]["summary"]

    async def test_statuses_error(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "statuses-error-state",
                    "conditions": ["check-failure=sick-ci"],
                    "actions": {"post_check": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        sorted_checks = sorted(
            await ctxt.pull_engine_check_runs, key=operator.itemgetter("name")
        )
        assert len(sorted_checks) == 2
        assert "failure" == sorted_checks[0]["conclusion"]

        await self.create_status(p, "sick-ci", "error")
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        sorted_checks = sorted(
            await ctxt.pull_engine_check_runs, key=operator.itemgetter("name")
        )
        assert len(sorted_checks) == 2
        assert "success" == sorted_checks[0]["conclusion"]

    async def test_linear_history(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "noways",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "-linear-history",
                    ],
                    "actions": {"close": {}},
                },
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()
        await self.merge_pull(p1["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.client_admin.put(
            f"{self.repository_ctxt.base_url}/pulls/{p2['number']}/update-branch",
            api_version="lydian",
            json={"expected_head_sha": p2["head"]["sha"]},
        )
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

    async def test_queued(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=ready-to-merge",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
                {
                    "name": "Add queued label",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "queue-position>=0",
                    ],
                    "actions": {"label": {"add": ["queued"]}},
                },
                {
                    "name": "Remove queued label",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "queue-position=-1",
                    ],
                    "actions": {"label": {"remove": ["queued"]}},
                },
            ],
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()

        await self.add_label(p["number"], "ready-to-merge")
        await self.run_engine()
        p = await self.get_pull(p["number"])
        assert "queued" in [label["name"] for label in p["labels"]]

        await self.remove_label(p["number"], "ready-to-merge")
        await self.run_engine()
        p = await self.get_pull(p["number"])
        assert "queued" not in [label["name"] for label in p["labels"]]
