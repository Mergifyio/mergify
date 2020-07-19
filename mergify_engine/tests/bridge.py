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

import argparse
import json
import logging
import os
import time

from mergify_engine import logs
from mergify_engine import utils
from mergify_engine.clients import http


LOG = logging.getLogger(__name__)


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--dest", default="http://localhost:8802/event")

    args = parser.parse_args()

    logs.setup_logging()

    session = http.Client()

    payload_data = os.urandom(250)
    payload_hmac = utils.compute_hmac(payload_data)

    if args.clean:
        # httpx doesn't allow data= here yet: https://github.com/encode/httpx/pull/900
        r = session.request(
            "DELETE",
            "https://gh.mergify.io/events-testing",
            data=payload_data,
            headers={"X-Hub-Signature": "sha1=" + payload_hmac},
        )
        r.raise_for_status()

    while True:
        try:
            # httpx doesn't allow data= here yet: https://github.com/encode/httpx/pull/900
            resp = session.request(
                "GET",
                "https://gh.mergify.io/events-testing",
                data=payload_data,
                headers={"X-Hub-Signature": "sha1=" + payload_hmac},
            )
            events = resp.json()
            for event in reversed(events):
                LOG.info("")
                LOG.info("==================================================")
                LOG.info(
                    ">>> GOT EVENT: %s %s/%s",
                    event["id"],
                    event["type"],
                    event["payload"].get("state", event["payload"].get("action")),
                )
                data = json.dumps(event["payload"])
                hmac = utils.compute_hmac(data.encode("utf8"))
                session.post(
                    args.dest,
                    headers={
                        "X-GitHub-Event": event["type"],
                        "X-GitHub-Delivery": event["id"],
                        "X-Hub-Signature": "sha1=%s" % hmac,
                        "Content-type": "application/json",
                    },
                    data=data,
                )
        except Exception:
            LOG.error("event handling failure", exc_info=True)
        time.sleep(1)


if __name__ == "__main__":
    run()
