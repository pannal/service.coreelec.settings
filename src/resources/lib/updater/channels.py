# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)

"""The three p3i update channels, and the gate on the internal one.

The internal channel's Mega link is never stored in this repository in the
clear. Only ciphertext ships; the password derives the key that opens it.

What this protects and what it does not: the git tree and the addon zip in the
wild carry no usable link, so distribution is protected. An unlocked box is
not - once a tester enters the password the decrypted link is written to the
settings file so background update checks keep working, and anyone with root on
that box can read it back. Rotating means a new Mega folder, a new blob, and an
addon update.
"""

import base64
import hashlib
import os

from Crypto.Cipher import AES

RELEASE = 'Release'
TESTING = 'Testing'
INTERNAL = 'Internal'

CHANNELS = (RELEASE, TESTING, INTERNAL)
DEFAULT_CHANNEL = RELEASE

GITHUB_REPO = 'pannal/CoreELEC'

TESTING_LINK = 'https://mega.nz/folder/mA0AAIRL#huzd7S1XCEYSQXPEcR_rHw'

# Sealed internal channel link. Generate with:
#     tools/seal_channel.py 'https://mega.nz/folder/<id>#<key>'
# and paste the single line it prints here. Empty means the internal channel is
# not provisioned; it is then reported as unavailable rather than failing oddly.
#
# The password is short, so scrypt's cost is doing most of the work here. That
# raises the price of each guess; it does not make a guessable password safe.
# Treat this as a speed bump against casual redistribution of the link, not as
# a real access control.
#
# Rotating the password alone does NOT undo an exposure. What this seals is the
# Mega folder key itself, so anyone who has ever opened the blob keeps access to
# that folder permanently. A real rotation means creating a NEW Mega folder,
# moving the builds across, and sealing that link instead.
INTERNAL_BLOB = 'AZqhOnUw/dLootg3pst8cxk6Ra2xSu4RdcFyD7JNH0e8ZYEqf2YZlgh7rdr0F8XhPJDC7MBmDzTiDUtcUnXxhTp5LgRXnngQ3FQ/A4QX+YECC+qyITekNTqTR8UieDKBNUkV'

BLOB_VERSION = 1
_SALT_LEN = 16
_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_LEN = 32

# 128 * N * r = 16 MiB, roughly 200-400 ms on an S922X. Password strength is the
# real boundary; this only sets the cost of each guess.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class WrongPassword(Exception):
    """Raised when a blob fails to authenticate under the supplied password."""


class NotProvisioned(Exception):
    """Raised when the internal channel has no sealed blob compiled in."""


def _derive(password, salt):
    return hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_KEY_LEN,
        )


def seal(link, password, salt=None, nonce=None):
    """Encrypt a channel link under a password. Returns one base64 line."""
    salt = salt if salt is not None else os.urandom(_SALT_LEN)
    nonce = nonce if nonce is not None else os.urandom(_NONCE_LEN)
    key = _derive(password, salt)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(link.encode('utf-8'))
    raw = bytes([BLOB_VERSION]) + salt + nonce + ciphertext + tag
    return base64.b64encode(raw).decode('ascii')


def unseal(blob, password):
    """Recover a channel link. Raises WrongPassword on a bad password or blob.

    GCM's authentication tag is the password check - there is no separately
    stored hash to leak.
    """
    if not blob:
        raise NotProvisioned('no sealed link compiled in')
    try:
        raw = base64.b64decode(blob)
    except (ValueError, TypeError) as exc:
        raise WrongPassword('malformed blob') from exc

    header = 1 + _SALT_LEN + _NONCE_LEN
    if len(raw) < header + _TAG_LEN + 1 or raw[0] != BLOB_VERSION:
        raise WrongPassword('malformed blob')

    salt = raw[1:1 + _SALT_LEN]
    nonce = raw[1 + _SALT_LEN:header]
    ciphertext = raw[header:-_TAG_LEN]
    tag = raw[-_TAG_LEN:]

    key = _derive(password, salt)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        link = cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as exc:
        raise WrongPassword('wrong password') from exc
    return link.decode('utf-8')


def resolve_channel(value):
    """Coerce a stored Channel setting to a channel we can actually serve.

    Existing installs hold a CoreELEC train name here ('21-ng'), which matches
    nothing. Handled at read time rather than by a migration pass so that a
    hand-edited or truncated value is corrected too.
    """
    return value if value in CHANNELS else DEFAULT_CHANNEL


def is_provisioned(channel):
    """False only for an internal channel with no blob compiled in."""
    if channel != INTERNAL:
        return True
    return bool(INTERNAL_BLOB)
