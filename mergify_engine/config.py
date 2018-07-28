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


import os
import re

import daiquiri

import yaml

LOG = daiquiri.getLogger(__name__)


with open(os.getenv("MERGIFYENGINE_SETTINGS", "fake.yml")) as f:
    CONFIG = yaml.safe_load(f.read())

globals().update(CONFIG)


def log():
    LOG.info("##################### CONFIGURATION ######################")
    for name, value in CONFIG.items():
        if (name in ["PRIVATE_KEY", "WEBHOOK_SECRET", "OAUTH_CLIENT_ID",
                     "OAUTH_CLIENT_SECRET", "MAIN_TOKEN", "FORK_TOKEN"]
                and value is not None):
            value = "*****"
        if "URL" in name:
            value = re.sub(r'://[^@]*@', "://*****@", value)
        LOG.info("* MERGIFYENGINE_%s: %s", name, value)
    LOG.info("##########################################################")
