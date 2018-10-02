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
    cancel_in_progress = True

    @property
    @staticmethod
    @abc.abstractmethod
    def validator():
        pass

    @abc.abstractmethod
    def __call__(self, installation_id, installation_token, subscription,
                 event_type, data, pull):
        pass
