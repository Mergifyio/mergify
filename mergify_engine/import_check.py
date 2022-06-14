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

# NOTE(sileht): This should mimic the worker and asgi app as much as possible.


def import_check_worker() -> int:
    from mergify_engine import config  # noqa isort:skip
    from mergify_engine import worker  # noqa isort:skip

    worker.service.setup("import-check-worker")
    worker.signals.register()

    return 0


def import_check_web() -> int:
    from mergify_engine import config  # noqa isort:skip
    from mergify_engine.web import asgi

    asgi.service.setup("import-check-web")

    return 0
