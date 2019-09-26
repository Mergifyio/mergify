# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Julien Danjou <jd@mergify.io>
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

from mergify_engine import web


def test_badge_redirect():
    with web.app.test_request_context("/"):
        reply = web._get_badge_url("mergifyio", "mergify-engine", "png")
        assert reply.status_code == 302
        assert reply.headers["Location"].startswith(
            "https://img.shields.io/badge/Mergify-disabled-critical.png?"
            "style=flat&logo=data:image/png;base64,iVBORw0KGgoAAA"
        )
        assert len(reply.headers["Location"]) == 2933

        reply = web._get_badge_url("mergifyio", "mergify-engine", "svg")
        assert reply.status_code == 302
        assert reply.headers["Location"].startswith(
            "https://img.shields.io/badge/Mergify-disabled-critical.svg?"
            "style=flat&logo=data:image/png;base64,iVBORw0KGgoAAA"
        )
        assert len(reply.headers["Location"]) == 2933


def test_badge_endpoint():
    with web.app.test_request_context("/"):
        reply = web.badge("mergifyio", "mergify-engine")
        assert reply.status_code == 200
        assert reply.json == {
            "schemaVersion": 1,
            "label": "Mergify",
            "color": "critical",
            "message": "disabled",
            "logoSvg": web._MERGIFY_LOGO_SVG,
        }
