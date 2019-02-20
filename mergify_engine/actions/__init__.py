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

import abc

import attr


@attr.s
class Action(abc.ABC):
    config = attr.ib()
    cancelled_check_report = (
        "cancelled", "The rule doesn't match anymore, this action "
        "has been cancelled", "")

    # If an action can't be twice in a rule this must be set to true
    only_once = False

    @property
    @staticmethod
    @abc.abstractmethod
    def validator():  # pragma: no cover
        pass

    @staticmethod
    def run(installation_id, installation_token,
            event_type, data, pull, missing_conditions):  # pragma: no cover
        pass

    @staticmethod
    def cancel(installation_id, installation_token,
               event_type, data, pull, missing_conditions):  # pragma: no cover
        pass
