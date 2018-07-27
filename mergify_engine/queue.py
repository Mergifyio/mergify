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

import rq
import uhashring

from mergify_engine import config
from mergify_engine import utils
from mergify_engine import worker

# TODO(sileht): Make the ring dynamic
global RING
nodes = []
for fqdn, w in sorted(config.TOPOLOGY.items()):
    nodes.extend(map(lambda x: "%s-%003d" % (fqdn, x), range(w)))

RING = uhashring.HashRing(nodes=nodes)


def get_queue(slug, subscription):
    global RING
    name = "%s-%s" % (RING.get_node(slug),
                      "high" if subscription["subscribed"] else "low")
    return rq.Queue(name, connection=utils.get_redis_for_rq())


# Lightweight pickle object for rq
def dispatch(message_kind, *args, **kwargs):
    method = getattr(worker.MergifyWorker, "job_%s" % message_kind)
    return method(*args, **kwargs)


def route(repo, subscription, message_kind, *args, **kwargs):
    q = get_queue(repo, subscription)
    q.enqueue(dispatch, message_kind, *args, **kwargs)


def publish(message_kind, *args, **kwargs):
    q = rq.Queue("incoming-events", connection=utils.get_redis_for_rq())
    q.enqueue(dispatch, message_kind, *args, **kwargs)
