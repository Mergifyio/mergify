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

import base64
import binascii
import json
import os

import cryptography
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import ciphers
from cryptography.hazmat.primitives import hashes

import daiquiri

import requests

from mergify_engine import config


LOG = daiquiri.getLogger(__name__)

digest = hashes.Hash(hashes.SHA256(), backend=default_backend())
digest.update(config.CACHE_TOKEN_SECRET.encode())

SECRET_KEY = digest.finalize()
IV_BYTES_NEEDED = 12
BLOCK_SIZE = 16
TAG_SIZE_BYTES = BLOCK_SIZE


def _encrypt(sub):
    value = json.dumps(sub).encode()
    iv = os.urandom(IV_BYTES_NEEDED)
    cipher = ciphers.Cipher(
        ciphers.algorithms.AES(SECRET_KEY),
        ciphers.modes.GCM(iv),
        backend=default_backend()
    )
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(value) + encryptor.finalize()
    encrypted = base64.b64encode(iv + encryptor.tag + encrypted)
    return encrypted


def _decrypt(value):
    try:
        decrypted = base64.b64decode(value)
    except binascii.Error:
        LOG.error("Invalid encrypted token: invalid base64")
        return

    if len(decrypted) < IV_BYTES_NEEDED + TAG_SIZE_BYTES:
        LOG.error("Invalid encrypted token: size check failure")
        return

    iv = decrypted[:IV_BYTES_NEEDED]
    tag = decrypted[IV_BYTES_NEEDED: IV_BYTES_NEEDED + TAG_SIZE_BYTES]
    decrypted = decrypted[IV_BYTES_NEEDED + TAG_SIZE_BYTES:]
    cipher = ciphers.Cipher(
        ciphers.algorithms.AES(SECRET_KEY),
        ciphers.modes.GCM(iv, tag),
        backend=default_backend()
    )
    decryptor = cipher.decryptor()
    try:
        decrypted = decryptor.update(decrypted) + decryptor.finalize()
    except cryptography.exceptions.InvalidTag:
        LOG.error("Invalid encrypted token: decryptor() failure")
        return

    try:
        decrypted = decrypted.decode()
    except UnicodeDecodeError:
        LOG.error("Invalid encrypted token: decode() failure")
        return

    try:
        return json.loads(decrypted)
    except json.JSONDecodeError:
        LOG.error("Invalid encrypted token: json.load() failure")
        return


def _retrieve_subscription_from_db(installation_id):
    LOG.debug("Subscription not cached, retrieving it...",
              install_id=installation_id)
    resp = requests.get(config.SUBSCRIPTION_URL %
                        installation_id,
                        auth=(config.OAUTH_CLIENT_ID,
                              config.OAUTH_CLIENT_SECRET))
    if resp.status_code == 404:
        sub = {
            "tokens": {},
            # FIXME we need to know if install is unknown or token is wrong
            "subscription_active": False,
            "subscription_reason": "404 returned by engine",
            "subscription_cost": 0,
        }
    elif resp.status_code == 200:
        sub = resp.json()
        sub["tokens"] = dict((login, token["access_token"])
                             for login, token in sub["tokens"].items())
    else:  # pragma: no cover
        # NOTE(sileht): handle this better
        resp.raise_for_status()
    return sub


def _retrieve_subscription_from_cache(r, installation_id):
    encrypted_sub = r.get("subscription-cache-%s" % installation_id)
    if encrypted_sub:
        return _decrypt(encrypted_sub)


def _save_subscription_to_cache(r, installation_id, sub):
    encrypted = _encrypt(sub)
    r.set("subscription-cache-%s" % installation_id, encrypted, ex=3600)


def get_subscription(r, installation_id):
    sub = _retrieve_subscription_from_cache(r, installation_id)
    # TODO(sileht): We can remove the "tokens" check when we have flush
    # the all tokens cache
    if not sub or "tokens" not in sub:
        sub = _retrieve_subscription_from_db(installation_id)
        _save_subscription_to_cache(r, installation_id, sub)
    return sub
