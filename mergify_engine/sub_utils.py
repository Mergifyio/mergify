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

from mergify_engine import config
from mergify_engine import utils
from mergify_engine.clients import http


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
        backend=default_backend(),
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
    tag = decrypted[IV_BYTES_NEEDED : IV_BYTES_NEEDED + TAG_SIZE_BYTES]
    decrypted = decrypted[IV_BYTES_NEEDED + TAG_SIZE_BYTES :]
    cipher = ciphers.Cipher(
        ciphers.algorithms.AES(SECRET_KEY),
        ciphers.modes.GCM(iv, tag),
        backend=default_backend(),
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


async def _retrieve_subscription_from_db(installation_id):
    LOG.info("Subscription not cached, retrieving it...", install_id=installation_id)
    async with http.AsyncClient() as client:
        try:
            resp = await client.get(
                config.SUBSCRIPTION_URL % installation_id,
                auth=(config.OAUTH_CLIENT_ID, config.OAUTH_CLIENT_SECRET),
            )
        except http.HTTPNotFound as e:
            sub = {
                "tokens": {},
                "subscription_active": False,
                "subscription_reason": e.message,
            }
        else:
            sub = resp.json()
            sub["tokens"] = dict(
                (login, token["access_token"]) for login, token in sub["tokens"].items()
            )
    return sub


async def _retrieve_subscription_from_cache(installation_id):
    r = await utils.get_aredis_for_cache()
    encrypted_sub = await r.get("subscription-cache-%s" % installation_id)
    if encrypted_sub:
        return _decrypt(encrypted_sub)


async def save_subscription_to_cache(installation_id, sub):
    r = await utils.get_aredis_for_cache()
    encrypted = _encrypt(sub)
    await r.setex("subscription-cache-%s" % installation_id, 3600, encrypted)


async def get_subscription(installation_id):
    sub = await _retrieve_subscription_from_cache(installation_id)
    if sub is None:
        sub = await _retrieve_subscription_from_db(installation_id)
        await save_subscription_to_cache(installation_id, sub)
    return sub
