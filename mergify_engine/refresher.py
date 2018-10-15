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
import sys

import requests

from mergify_engine import config
from mergify_engine import utils


def refresh(slug=None):
    data = os.urandom(250)
    hmac = utils.compute_hmac(data)

    url = config.BASE_URL + "/refresh"
    if slug:
        url = url + "/" + slug
    r = requests.post(url,
                      headers={"X-Hub-Signature": "sha1=" + hmac},
                      data=data)
    r.raise_for_status()
    print(r.text)


def main():
    parser = argparse.ArgumentParser(
        description='Force refresh of mergify_engine'
    )
    parser.add_argument(
        "--all", action='store_true', help="Refresh *everything*")
    parser.add_argument(
        "slug", nargs="*",
        help="<owner>/<repo>/branch/<branch> or <owner>/<repo>/pull/<pull#>")

    args = parser.parse_args()

    if args.slug:
        for slug in args.slug:
            refresh(slug)
    elif args.all:
        refresh()
    else:
        parser.print_help()


if __name__ == '__main__':
    sys.exit(main())
