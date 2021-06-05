# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2021 Mergify SAS
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

from unittest import mock

import yaml

from mergify_engine import config
from mergify_engine import debug
from mergify_engine import subscription
from mergify_engine.tests.functional import base


class TestDebugger(base.FunctionalTestBase):
    async def test_debugger(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        {
                            "or": [
                                "label=doubt",
                                "label=suspect",
                            ]
                        },
                        {
                            "and": [
                                "number>0",
                                "title~=pull request",
                            ]
                        },
                    ],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        # Enable one feature to see the debug output
        self.subscription.features = frozenset([subscription.Features.PRIORITY_QUEUES])

        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()

        await self.run_engine()

        # NOTE(sileht): Run is a thread to not mess with the main asyncio loop
        with mock.patch("sys.stdout") as stdout:
            await debug.report(p["html_url"])
            s1 = "".join(call.args[0] for call in stdout.write.mock_calls)

        with mock.patch("sys.stdout") as stdout:
            await debug.report(p["base"]["repo"]["html_url"])
            s2 = "".join(call.args[0] for call in stdout.write.mock_calls)

        with mock.patch("sys.stdout") as stdout:
            await debug.report(p["base"]["user"]["html_url"])
            s3 = "".join(call.args[0] for call in stdout.write.mock_calls)

        assert s1.startswith(s2)

        assert (
            s1.strip()
            == f"""* INSTALLATION ID: {config.INSTALLATION_ID}
* SUBSCRIBED (cache/db): False / False
* Features (cache):
  - priority_queues
* Features (db):
  - priority_queues
* ENGINE-CACHE SUB DETAIL: You're not nice
* ENGINE-CACHE SUB NUMBER OF TOKENS: 2 (mergify-test1, mergify-test3)
* ENGINE-CACHE SUB: MERGIFY INSTALLED AND ENABLED ON THIS REPOSITORY
* DASHBOARD SUB DETAIL: You're not nice
* DASHBOARD SUB NUMBER OF TOKENS: 2 (mergify-test1, mergify-test3)
* DASHBOARD SUB: MERGIFY INSTALLED AND ENABLED ON THIS REPOSITORY
* WORKER: Installation not queued to process
* REPOSITORY IS PUBLIC
* CONFIGURATION:
Config filename: .mergify.yml
pull_request_rules:
- actions:
    comment:
      message: WTF?
  conditions:
  - base={self.master_branch_name}
  - or:
    - label=doubt
    - label=suspect
  - and:
    - number>0
    - title~=pull request
  name: comment

* QUEUES: 
* TRAIN: 
* PULL REQUEST:
{{'approved-reviews-by': [],
 'assignee': [],
 'author': 'mergify-test2',
 'base': '{self.master_branch_name}',
 'body': 'test_debugger: pull request n1 from fork',
 'changes-requested-reviews-by': [],
 'check-failure': [],
 'check-neutral': [],
 'check-pending': [],
 'check-skipped': [],
 'check-stale': [],
 'check-success': ['Summary'],
 'check-success-or-neutral': ['Summary'],
 'closed': False,
 'commented-reviews-by': [],
 'conflict': False,
 'dismissed-reviews-by': [],
 'files': ['test1'],
 'head': '{p['head']['ref']}',
 'label': [],
 'locked': False,
 'merged': False,
 'merged-by': '',
 'milestone': '',
 'number': {p['number']},
 'review-requested': [],
 'status-failure': [],
 'status-neutral': [],
 'status-success': ['Summary'],
 'title': 'test_debugger: pull request n1 from fork'}}
is_behind: False
mergeable_state: clean
* MERGIFY LAST CHECKS:
[Summary]: success | 1 potential rule
> ### Rule: comment (comment)
> - [X] `base={self.master_branch_name}`
> - [ ] any of:
>   - [ ] `label=doubt`
>   - [ ] `label=suspect`
> - [X] all of:
>   - [X] `number>0`
>   - [X] `title~=pull request`
> 
> <hr />
> :sparkling_heart:&nbsp;&nbsp;Mergify is proud to provide this service for free to open source projects.
> 
> :rocket:&nbsp;&nbsp;You can help us by [becoming a sponsor](/sponsors/Mergifyio)!
> <hr />
> 
> <details>
> <summary>Mergify commands and options</summary>
> 
> <br />
> 
> More conditions and actions can be found in the [documentation](https://docs.mergify.io/).
> 
> You can also trigger Mergify actions by commenting on this pull request:
> 
> - `@Mergifyio refresh` will re-evaluate the rules
> - `@Mergifyio rebase` will rebase this PR on its base branch
> - `@Mergifyio update` will merge the base branch into this PR
> - `@Mergifyio backport <destination>` will backport this PR on `<destination>` branch
> 
> Additionally, on Mergify [dashboard](https://dashboard.mergify.io/) you can:
> 
> - look at your merge queues
> - generate the Mergify configuration with the config editor.
> 
> Finally, you can contact us on https://mergify.io/
> </details>
> <!-- J1J1bGU6IGNvbW1lbnQgKGNvbW1lbnQpJzogbmV1dHJhbAo= -->
* MERGIFY LIVE MATCHES:
[Summary]: success | 1 potential rule
> ### Rule: comment (comment)
> - [X] `base={self.master_branch_name}`
> - [ ] any of:
>   - [ ] `label=doubt`
>   - [ ] `label=suspect`
> - [X] all of:
>   - [X] `number>0`
>   - [X] `title~=pull request`
> 
> <hr />
> :sparkling_heart:&nbsp;&nbsp;Mergify is proud to provide this service for free to open source projects.
> 
> :rocket:&nbsp;&nbsp;You can help us by [becoming a sponsor](/sponsors/Mergifyio)!
> <hr />"""  # noqa:W291
        )

        assert (
            s3.strip()
            == f"""* INSTALLATION ID: {config.INSTALLATION_ID}
* SUBSCRIBED (cache/db): False / False
* Features (cache):
  - priority_queues
* Features (db):
  - priority_queues
* ENGINE-CACHE SUB DETAIL: You're not nice
* ENGINE-CACHE SUB NUMBER OF TOKENS: 2 (mergify-test1, mergify-test3)
* DASHBOARD SUB DETAIL: You're not nice
* DASHBOARD SUB NUMBER OF TOKENS: 2 (mergify-test1, mergify-test3)
* WORKER: Installation not queued to process"""
        )
