# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
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
import os

import requests

from mergify_engine import config
from mergify_engine import utils


def api_call(url, method="post"):
    data = os.urandom(250)
    hmac = utils.compute_hmac(data)

    r = requests.request(method,
                         url,
                         headers={"X-Hub-Signature": "sha1=" + hmac},
                         data=data)
    r.raise_for_status()
    print(r.text)


def clear_token_cache():
    parser = argparse.ArgumentParser(
        description='Force refresh of installation token'
    )
    parser.add_argument("installation_id")
    args = parser.parse_args()
    api_call(config.BASE_URL + "/subscription-cache/%s" % args.installation_id,
             method="delete")


def refresher():
    parser = argparse.ArgumentParser(
        description='Force refresh of mergify_engine'
    )
    parser.add_argument(
        "--all", action='store_true', help="Refresh *everything*")
    parser.add_argument(
        "urls", nargs="*",
        help=("<owner>/<repo>/branch/<branch>, <owner>/<repo>/pull/<pull#> "
              "or https://github.com/<owner>/<repo>/pull/<pull#>"))

    args = parser.parse_args()

    if args.urls:
        for url in args.urls:
            api_call(config.BASE_URL + "/refresh/" +
                     url.replace("https://github.com/", ""))
    elif args.all:
        api_call(config.BASE_URL + "/refresh")
    else:
        parser.print_help()


def queues():
    parser = argparse.ArgumentParser(
        description='Show queue of mergify_engine'
    )
    parser.add_argument("installation_id")

    args = parser.parse_args()
    api_call(config.BASE_URL + "/queues/%s" % args.installation_id,
             method="GET")
