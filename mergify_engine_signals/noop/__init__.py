# -*- encoding: utf-8 -*-
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

import typing

from mergify_engine import context
from mergify_engine import signals


class Signal(signals.SignalBase):
    async def __call__(
        self,
        ctxt: context.Context,
        event: signals.EventName,
        metadata: typing.Optional[signals.SignalMetadata],
    ) -> None:
        pass
