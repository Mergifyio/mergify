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

import voluptuous

from mergify_engine import actions
from mergify_engine import backports


class BackportAction(actions.Action):
    validator = {voluptuous.Required("branches", default=[]): [str]}

    def __call__(self, installation_id, installation_token, subscription,
                 event_type, data, pull):
        # TODO(sileht): some error should be report in Github UI

        detail = "The following pull requests have been created: "
        for branch in self.config['branches']:
            new_pull = backports.backport(pull, branch, installation_token)
            if not new_pull:
                raise Exception("backport of %s to %s have failed" %
                                (pull, branch))
            detail += "\n* [#%d %s](%s)" % (new_pull.number, new_pull.title,
                                            new_pull.html_url)
        return "success", detail
