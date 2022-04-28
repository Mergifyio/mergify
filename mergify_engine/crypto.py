# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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
import os
import typing

import cryptography.exceptions
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import ciphers
from cryptography.hazmat.primitives import hashes
from datadog import statsd

from mergify_engine import config


digest_current = hashes.Hash(hashes.SHA256(), backend=default_backend())
digest_current.update(config.CACHE_TOKEN_SECRET.encode())
SECRET_KEY = digest_current.finalize()
del digest_current

SECRET_KEY_OLD: typing.Optional[bytes]

if config.CACHE_TOKEN_SECRET_OLD:
    digest_old = hashes.Hash(hashes.SHA256(), backend=default_backend())
    digest_old.update(config.CACHE_TOKEN_SECRET_OLD.encode())
    SECRET_KEY_OLD = digest_old.finalize()
    del digest_old
else:
    SECRET_KEY_OLD = None

IV_BYTES_NEEDED = 12
BLOCK_SIZE = 16
TAG_SIZE_BYTES = BLOCK_SIZE


class CryptoError(Exception):
    pass


def encrypt(value: bytes) -> bytes:
    """Encrypt a string.

    :param: An encrypted string."""
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


def _decrypt(iv: bytes, tag: bytes, value: bytes, secret: bytes) -> bytes:
    cipher = ciphers.Cipher(
        ciphers.algorithms.AES(secret),
        ciphers.modes.GCM(iv, tag),
        backend=default_backend(),
    )
    decryptor = cipher.decryptor()
    return decryptor.update(value) + decryptor.finalize()


def decrypt(value: bytes) -> bytes:
    """Decrypt a string.

    :return: A decrypted string."""
    try:
        decoded = base64.b64decode(value)
    except binascii.Error:
        raise CryptoError("Invalid encrypted token: invalid base64")

    if len(decoded) < IV_BYTES_NEEDED + TAG_SIZE_BYTES:
        raise CryptoError("Invalid encrypted token: size check failure")

    iv = decoded[:IV_BYTES_NEEDED]
    tag = decoded[IV_BYTES_NEEDED : IV_BYTES_NEEDED + TAG_SIZE_BYTES]
    value = decoded[IV_BYTES_NEEDED + TAG_SIZE_BYTES :]

    try:
        try:
            return _decrypt(iv, tag, value, SECRET_KEY)
        except cryptography.exceptions.InvalidTag:
            if SECRET_KEY_OLD is not None:
                statsd.increment("engine.crypto.old-secret-used")
                return _decrypt(iv, tag, value, SECRET_KEY_OLD)
            raise
    except cryptography.exceptions.InvalidTag:
        raise CryptoError("Invalid encrypted token: decryptor() failure")
