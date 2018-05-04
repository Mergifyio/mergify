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


import logging
import os
import yaml

LOG = logging.getLogger(__name__)


with open("config.yml") as f:
    CONFIG = yaml.load(f.read())

global cfg_msg
cfg_msg = ""
for name, config_value in CONFIG.items():
    name = name.upper()
    value = os.getenv("MERGIFYENGINE_%s" % name, config_value)
    if value == "<required>":
        raise RuntimeError("MERGIFYENGINE_%s environement of %s configuration"
                           "option must be set." % (name, name.lower()))
    if config_value is not None and value is not None:
        value = type(config_value)(value)
    globals()[name] = value

    if (name in ["PRIVATE_KEY", "WEBHOOK_SECRET"]
            and value is not None):
        value = "*****"
    cfg_msg += "* MERGIFYENGINE_%s: %s\n" % (name, value)


def log():
    global cfg_msg
    LOG.info("""
##################### CONFIGURATION ######################
%s##########################################################
""" % cfg_msg)
