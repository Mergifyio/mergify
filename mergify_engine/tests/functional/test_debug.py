from concurrent import futures
from unittest import mock

import yaml

from mergify_engine import debug
from mergify_engine import subscription
from mergify_engine.tests.functional import base


class TestDebugger(base.FunctionalTestBase):
    def test_debugger(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        # Enable one feature to see the debug output
        self.subscription.features = frozenset([subscription.Features.PRIORITY_QUEUES])

        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()

        self.run_engine()

        # NOTE(sileht): Run is a thread to not mess with the main asyncio loop
        with mock.patch("sys.stdout") as stdout:
            with futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(debug.report, p.html_url).result()

            s1 = "".join(call.args[0] for call in stdout.write.mock_calls)

        with mock.patch("sys.stdout") as stdout:
            with futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(debug.report, p.base.repo.html_url).result()

            s2 = "".join(call.args[0] for call in stdout.write.mock_calls)

        with mock.patch("sys.stdout") as stdout:
            with futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(debug.report, p.base.user.html_url).result()

            s3 = "".join(call.args[0] for call in stdout.write.mock_calls)

        assert s1.startswith(s2)

        assert (
            s1.strip()
            == f"""* INSTALLATION ID: 499592
* SUBSCRIBED (cache/db): False / False
* Features (cache):
  - priority_queues
* ENGINE-CACHE SUB DETAIL: You're not nice
* ENGINE-CACHE SUB NUMBER OF TOKENS: 1 (mergify-test1)
* ENGINE-CACHE SUB: MERGIFY INSTALLED AND ENABLED ON THIS REPOSITORY
* DASHBOARD SUB DETAIL: You're not nice
* DASHBOARD SUB NUMBER OF TOKENS: 1 (mergify-test1)
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
  name: comment

* QUEUES: 
* PULL REQUEST:
{{'approved-reviews-by': [],
 'assignee': [],
 'author': 'mergify-test2',
 'base': '{self.master_branch_name}',
 'body': 'Pull request n1 from fork',
 'changes-requested-reviews-by': [],
 'check-failure': [],
 'check-neutral': [],
 'check-success': ['Summary'],
 'closed': False,
 'commented-reviews-by': [],
 'conflict': False,
 'dismissed-reviews-by': [],
 'files': ['test1'],
 'head': '{p.head.ref}',
 'label': [],
 'locked': False,
 'merged': False,
 'merged-by': '',
 'milestone': '',
 'number': {p.number},
 'review-requested': [],
 'status-failure': [],
 'status-neutral': [],
 'status-success': ['Summary'],
 'title': 'Pull request n1 from fork'}}
is_behind: False
mergeable_state: clean
* MERGIFY LAST CHECKS:
[Summary]: success | 1 rule matches
> #### Rule: comment (comment)
> - [X] `base={self.master_branch_name}`
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
> <!-- J1J1bGU6IGNvbW1lbnQgKGNvbW1lbnQpJzogc3VjY2Vzcwo= -->
* MERGIFY LIVE MATCHES:
> 1 rule matches
#### Rule: comment (comment)
- [X] `base={self.master_branch_name}`

<hr />
:sparkling_heart:&nbsp;&nbsp;Mergify is proud to provide this service for free to open source projects.

:rocket:&nbsp;&nbsp;You can help us by [becoming a sponsor](/sponsors/Mergifyio)!
<hr />"""  # noqa:W291
        )

        assert (
            s3.strip()
            == """* INSTALLATION ID: 499592
* SUBSCRIBED (cache/db): False / False
* Features (cache):
  - priority_queues
* ENGINE-CACHE SUB DETAIL: You're not nice
* ENGINE-CACHE SUB NUMBER OF TOKENS: 1 (mergify-test1)
* ENGINE-CACHE SUB: MERGIFY DOESN'T HAVE ANY VALID OAUTH TOKENS
* DASHBOARD SUB DETAIL: You're not nice
* DASHBOARD SUB NUMBER OF TOKENS: 1 (mergify-test1)
* DASHBOARD SUB: MERGIFY DOESN'T HAVE ANY VALID OAUTH TOKENS
* WORKER: Installation not queued to process"""
        )
