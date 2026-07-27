# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)

"""Anonymous Mega folder-link client: list a folder, stream-decrypt one file.

Written against megatools-1.10.3 lib/mega.c, which is the reference
implementation for this path. Line references below point at that file.

Only what an update channel needs is implemented: no login, no upload, no
rename. Integrity is covered by the .sha256 sidecar published alongside every
build, so Mega's own chunked meta-MAC is deliberately not verified.
"""

import base64
import http.client
import json
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from Crypto.Cipher import AES

API_URL = 'https://g.api.mega.co.nz/cs'

# Strict shape, matching dl.c:367: 8-char folder id, 22-char (16-byte) key.
FOLDER_LINK_RE = re.compile(
    r'^https?://mega\.nz/folder/([A-Za-z0-9_-]{8})#([A-Za-z0-9_-]{22})$')

NODE_FILE = 0
NODE_FOLDER = 1

# A base64 node key of 46+ chars is RSA-wrapped and cannot be opened with the
# folder key (mega.c:1942).
RSA_KEY_MIN_LEN = 46

_TIMEOUT = 30
_MAX_ATTEMPTS = 4

# Not a Mega code: the request never got far enough to be answered. Kept out of
# the API's own numbering so it cannot collide with a code Mega adds later.
TRANSPORT_ERROR = -100

# The API answers with a bare negative integer instead of an object on failure.
API_ERRORS = {
    TRANSPORT_ERROR: 'network error',
    -1: 'internal error',
    -2: 'bad request',
    -3: 'try again',
    -4: 'rate limited',
    -6: 'too many requests',
    -9: 'folder not found',
    -11: 'access denied - link no longer valid',
    -12: 'already exists',
    -13: 'incomplete request',
    -15: 'session expired',
    -16: 'folder blocked',
    -17: 'Mega bandwidth limit reached',
    -18: 'temporarily unavailable',
    }

# Worth another attempt; everything else is terminal. A box on domestic wifi
# loses a connection far more often than the API asks it to back off, and both
# deserve the same treatment.
RETRYABLE = frozenset((-3, -4, -6, -18, TRANSPORT_ERROR))


class MegaError(Exception):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        text = API_ERRORS.get(code, f'error {code}')
        if detail:
            text = f'{text} ({detail})'
        super().__init__(text)

    @property
    def retryable(self):
        return self.code in RETRYABLE


def b64decode(data):
    """Mega's unpadded base64url."""
    if isinstance(data, str):
        data = data.encode('ascii')
    return base64.urlsafe_b64decode(data + b'=' * (-len(data) % 4))


def b64encode(data):
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def parse_folder_link(link):
    """'https://mega.nz/folder/<id>#<key>' -> (folder_id, 16-byte folder key)."""
    match = FOLDER_LINK_RE.match((link or '').strip())
    if not match:
        raise ValueError('not a Mega folder link')
    folder_id, key_b64 = match.groups()
    folder_key = b64decode(key_b64)
    if len(folder_key) != 16:
        raise ValueError('Mega folder key is not 16 bytes')
    return folder_id, folder_key


def unpack_node_key(node_key):
    """32-byte file key -> (16-byte AES key, 8-byte CTR nonce).

    mega.c:1226 unpack_node_key:
        aes_key  = node_key[0:16] XOR node_key[16:32]
        nonce    = node_key[16:24]
        meta_mac = node_key[24:32]   (unused here)
    """
    if len(node_key) != 32:
        raise ValueError('file key is not 32 bytes')
    aes_key = bytes(a ^ b for a, b in zip(node_key[:16], node_key[16:32]))
    return aes_key, node_key[16:24]


def decrypt_node_key(encrypted_b64, folder_key):
    """AES-128 over 16-byte blocks with no chaining, i.e. ECB (b64_aes128_decrypt)."""
    raw = b64decode(encrypted_b64)
    if not raw or len(raw) % 16:
        raise ValueError('node key length is not a multiple of the block size')
    return AES.new(folder_key, AES.MODE_ECB).decrypt(raw)


def decrypt_attributes(encrypted_b64, aes_key):
    """AES-128-CBC with a zero IV, yielding b'MEGA{"n":"<filename>"}'.

    Unreadable attributes are {}, never an exception. The caller uses that to
    skip one node; raising here would abort the whole folder listing over a
    single node we were never going to be able to read anyway.
    """
    if not isinstance(encrypted_b64, (str, bytes)) or not encrypted_b64:
        return {}
    try:
        raw = b64decode(encrypted_b64)
    except (ValueError, TypeError):
        return {}
    raw = raw[:len(raw) - (len(raw) % 16)]
    if not raw:
        return {}
    plain = AES.new(aes_key, AES.MODE_CBC, b'\0' * 16).decrypt(raw)
    plain = plain.rstrip(b'\0')
    if not plain.startswith(b'MEGA'):
        return {}
    try:
        return json.loads(plain[4:].decode('utf-8', 'replace'))
    except ValueError:
        return {}


def _key_candidates(k_field, share_handles=()):
    """Keys from 'k', the ones we hold a share key for first.

    'k' carries one '<handle>:<b64key>' pair per principal the node was shared
    with, joined by '/'. Only the pair whose handle matches the folder root
    opens under our folder key. Taking the first pair blindly is wrong: a pair
    belonging to another share still decrypts to a plausible 32-byte value, so
    the length checks below cannot tell them apart and the node ends up with
    unreadable attributes. megatools selects by handle for this reason
    (mega.c:1920-1932); the root handle is registered as the share key for an
    anonymous folder link (mega.c:2237).
    """
    preferred, fallback = [], []
    for part in (k_field or '').split('/'):
        if ':' not in part:
            continue
        handle, value = part.split(':', 1)
        (preferred if handle in share_handles else fallback).append(value)
    return preferred + fallback


@dataclass
class MegaNode:
    handle: str
    parent: str
    kind: int
    name: str
    size: int
    timestamp: int
    aes_key: bytes = b''
    nonce: bytes = b''


class MegaDownload:
    """Decrypting reader over a Mega temp URL. Drop-in for a urlopen response."""

    def __init__(self, response, aes_key, nonce, size):
        self._response = response
        self._cipher = AES.new(aes_key, AES.MODE_CTR, nonce=nonce)
        self.size = size

    def read(self, amount):
        chunk = self._response.read(amount)
        if not chunk:
            return b''
        return self._cipher.decrypt(chunk)

    def getheader(self, name, default=None):
        return self._response.headers.get(name, default)

    def close(self):
        try:
            self._response.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class MegaFolder:
    """A public Mega folder link, opened anonymously."""

    def __init__(self, link, opener=None, sleep=time.sleep):
        self.folder_id, self.folder_key = parse_folder_link(link)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._seq = random.randint(0, 0xFFFFFFF)

    def _request(self, payload):
        self._seq += 1
        url = f'{API_URL}?id={self._seq}&n={self.folder_id}'
        body = json.dumps([payload]).encode('utf-8')
        request = urllib.request.Request(
            url, data=body, headers={'Content-Type': 'application/json'})

        delay = 1.0
        last_error = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._opener(request, timeout=_TIMEOUT) as response:
                    parsed = json.loads(response.read().decode('utf-8'))
            except (OSError, http.client.HTTPException, ValueError) as exc:
                # OSError covers urllib's URLError as well as the timeouts and
                # resets that reach us as plain socket errors; HTTPException
                # covers IncompleteRead and RemoteDisconnected; ValueError is a
                # body that did not decode as the JSON we asked for, which is
                # what a truncated response or a captive portal looks like.
                last_error = MegaError(TRANSPORT_ERROR, str(exc))
            else:
                # The whole response, or its single element, may be a bare
                # negative integer standing in for an error.
                if isinstance(parsed, int):
                    last_error = MegaError(parsed)
                elif isinstance(parsed, list) and parsed and isinstance(parsed[0], int):
                    last_error = MegaError(parsed[0])
                elif isinstance(parsed, list) and parsed:
                    return parsed[0]
                else:
                    last_error = MegaError(-1, 'empty response')

            if not last_error.retryable:
                raise last_error
            # Backoff sits between attempts, not after the last one: by then
            # the outcome is settled and sleeping only delays the caller, which
            # on the poll thread is a shutdown waiting on us.
            if attempt == _MAX_ATTEMPTS - 1:
                break
            self._sleep(delay)
            delay *= 2
        raise last_error

    def list_nodes(self):
        """Every node in the folder. 'r':1 makes this the whole recursive tree."""
        result = self._request({'a': 'f', 'c': 1, 'r': 1})
        raw_nodes = result.get('f') or []
        if not raw_nodes:
            return []

        # mega.c:2231 - the first element is the folder root; everything else
        # hangs off it, directly or transitively. Its handle is also the share
        # key handle every node key is listed under (mega.c:2237).
        root_handle = raw_nodes[0].get('h')
        nodes = []
        for index, raw in enumerate(raw_nodes):
            node = self._parse_node(raw, is_root=(index == 0),
                                    share_handles=(root_handle,))
            if node is not None:
                nodes.append(node)
        for node in nodes:
            if node.handle == root_handle:
                node.parent = ''
        return nodes

    def list_root_files(self):
        """Files sitting directly in the folder root.

        Mandatory rather than an optimisation: the testing folder's root holds
        archive subfolders and test media, and the listing call above returns
        the entire tree in one response.
        """
        nodes = self.list_nodes()
        if not nodes:
            return []
        root_handle = None
        for node in nodes:
            if not node.parent:
                root_handle = node.handle
                break
        if root_handle is None:
            return []
        return [n for n in nodes
                if n.kind == NODE_FILE and n.parent == root_handle]

    def _open_node_key(self, raw, kind, share_handles):
        """(aes_key, nonce, name) for a node, or None if no key opens it.

        Each candidate is tried until one yields readable attributes, so a node
        shared several times is not lost just because another share's key
        happens to be listed first.
        """
        for encrypted_key in _key_candidates(raw.get('k'), share_handles):
            if len(encrypted_key) >= RSA_KEY_MIN_LEN:
                continue                          # RSA-wrapped (mega.c:1942)
            try:
                node_key = decrypt_node_key(encrypted_key, self.folder_key)
            except (ValueError, KeyError):
                continue
            if kind == NODE_FILE:
                if len(node_key) != 32:           # mega.c:1956
                    continue
                aes_key, nonce = unpack_node_key(node_key)
            else:
                if len(node_key) != 16:           # mega.c:1961
                    continue
                aes_key, nonce = node_key, b''
            name = decrypt_attributes(raw.get('a'), aes_key).get('n', '')
            if name:
                return aes_key, nonce, name
        return None

    def _parse_node(self, raw, is_root=False, share_handles=()):
        handle = raw.get('h') or ''
        kind = raw.get('t')
        if kind not in (NODE_FILE, NODE_FOLDER) and not is_root:
            return None

        aes_key = b''
        nonce = b''
        name = ''

        opened = self._open_node_key(raw, kind, share_handles)
        if opened is not None:
            aes_key, nonce, name = opened
        elif not is_root:
            # No key we hold opens this node: RSA-wrapped, keyless, or shared
            # only with principals we are not.
            return None

        return MegaNode(
            handle=handle,
            parent=raw.get('p') or '',
            kind=kind if kind is not None else NODE_FOLDER,
            name=name,
            size=raw.get('s') or 0,
            timestamp=raw.get('ts') or 0,
            aes_key=aes_key,
            nonce=nonce,
            )

    def resolve(self, node):
        """Temp download URL and authoritative size for a file node.

        'n:' addresses a node inside a folder link; 'p:' is for standalone file
        links (mega.c:4515 vs :4561). ssl:2 asks for an HTTPS URL - megatools
        sends ssl:0, which yields plain HTTP.
        """
        result = self._request({'a': 'g', 'g': 1, 'ssl': 2, 'n': node.handle})
        url = result.get('g')
        if not isinstance(url, str) or not url:
            raise MegaError(-1, 'no download url returned')
        return url, result.get('s') or node.size

    def open(self, node):
        """Open a decrypting stream over a file node."""
        if node.kind != NODE_FILE or not node.aes_key:
            raise ValueError('not a readable file node')
        url, size = self.resolve(node)
        response = self._opener(url, timeout=_TIMEOUT)
        return MegaDownload(response, node.aes_key, node.nonce, size)
