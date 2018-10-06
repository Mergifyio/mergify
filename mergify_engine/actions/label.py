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

import github

import voluptuous

from mergify_engine import actions


class LabelAction(actions.Action):
    validator = {voluptuous.Required("add", default=[]): [str],
                 voluptuous.Required("remove", default=[]): [str]}

    dedicated_check = False

    def __call__(self, installation_id, installation_token, subscription,
                 event_type, data, pull):

        all_label = [l.name for l in pull.g_pull.base.repo.get_labels()]
        for label in self.config['add']:
            if label not in all_label:
                color = '%06x' % random.randrange(16**6)
                pull.g_pull.base.repo.create_label(label, color)
        pull.g_pull.add_to_labels(*self.config['add'])

        pull_labels = [l.name for l in pull.g_pull.labels]
        for label in self.config['remove']:
            if label in pull_labels:
                try:
                    pull.g_pull.remove_from_labels(label)
                except github.GithubException as e:
                    if e.status == 400:
                        pass
                    raise
