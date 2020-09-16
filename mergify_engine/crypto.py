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

import cryptography.exceptions
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import ciphers
from cryptography.hazmat.primitives import hashes

from mergify_engine import config


digest = hashes.Hash(hashes.SHA256(), backend=default_backend())
digest.update(config.CACHE_TOKEN_SECRET.encode())

SECRET_KEY = digest.finalize()
IV_BYTES_NEEDED = 12
BLOCK_SIZE = 16
TAG_SIZE_BYTES = BLOCK_SIZE

del digest


class CryptoError(Exception):
    pass


def encrypt(value):
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


def decrypt(value):
    """Decrypt a string.

    :return: A decrypted string."""
    try:
        decrypted = base64.b64decode(value)
    except binascii.Error:
        raise CryptoError("Invalid encrypted token: invalid base64")

    if len(decrypted) < IV_BYTES_NEEDED + TAG_SIZE_BYTES:
        raise CryptoError("Invalid encrypted token: size check failure")

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
        return decryptor.update(decrypted) + decryptor.finalize()
    except cryptography.exceptions.InvalidTag:
        raise CryptoError("Invalid encrypted token: decryptor() failure")
