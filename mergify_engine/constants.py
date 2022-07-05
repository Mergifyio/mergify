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

MERGIFY_CONFIG_FILENAMES = [
    ".mergify.yml",
    ".mergify/config.yml",
    ".github/mergify.yml",
]

SUMMARY_NAME = "Summary"

MERGE_QUEUE_BRANCH_PREFIX = "mergify/merge-queue"
MERGE_QUEUE_SUMMARY_NAME = "Queue: Embarked in merge train"
CONFIGURATION_CHANGED_CHECK_NAME = "Configuration changed"
CONFIGURATION_DELETED_CHECK_NAME = "Configuration has been deleted"
CONFIGURATION_MUTIPLE_FOUND_SUMMARY_TITLE = (
    "Multiple Mergify configurations have been found in the repository"
)
INITIAL_SUMMARY_TITLE = "Your rules are under evaluation"

CHECKS_TIMEOUT_CONDITION_LABEL = "checks-are-on-time"

MERGIFY_OPENSOURCE_SPONSOR_DOC = (
    "<hr />\n"
    ":sparkling_heart:&nbsp;&nbsp;Mergify is proud to provide this service "
    "for free to open source projects.\n\n"
    ":rocket:&nbsp;&nbsp;You can help us by [becoming a sponsor](/sponsors/Mergifyio)!\n"
)
MERGIFY_MERGE_QUEUE_PULL_REQUEST_DOC = """

More informations about Mergify merge queue can be found in the [documentation](https://docs.mergify.com/actions/queue.html).

<details>
<summary>Mergify commands</summary>

<br />

You can also trigger Mergify actions by commenting on this pull request:

- `@Mergifyio refresh` will re-evaluate the queue rules

Additionally, on Mergify [dashboard](https://dashboard.mergify.com) you can:

- look at your merge queues
- generate the Mergify configuration with the config editor.

Finally, you can contact us on https://mergify.com
</details>
"""

MERGIFY_PULL_REQUEST_DOC = """
<details>
<summary>Mergify commands and options</summary>

<br />

More conditions and actions can be found in the [documentation](https://docs.mergify.com/).

You can also trigger Mergify actions by commenting on this pull request:

- `@Mergifyio refresh` will re-evaluate the rules
- `@Mergifyio rebase` will rebase this PR on its base branch
- `@Mergifyio update` will merge the base branch into this PR
- `@Mergifyio backport <destination>` will backport this PR on `<destination>` branch

Additionally, on Mergify [dashboard](https://dashboard.mergify.com/) you can:

- look at your merge queues
- generate the Mergify configuration with the config editor.

Finally, you can contact us on https://mergify.com
</details>
"""
