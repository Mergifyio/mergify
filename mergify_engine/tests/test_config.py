# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

import testtools
import yaml

from mergify_engine import rules


class TestConfig(testtools.TestCase):
    def test_config(self):
        with open("default_rule.yml", "r") as f:
            print(f.read())
            f.seek(0)
            config = yaml.load(f.read())

        config = {
            "rules": {
                "default": config,
                "branches": {
                    "stable/.*": config,
                    "stable/3.1": config,
                    "stable/foo": {
                        "automated_backport_labels": {
                            'bp-3.1': 'stable/3.1',
                            'bp-3.2': 'stable/4.2',
                        }
                    }
                }
            }
        }
        rules.validate_rule(yaml.dump(config))
