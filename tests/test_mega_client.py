# SPDX-License-Identifier: GPL-2.0-or-later
"""Mega folder-link parsing, key derivation, listing, and error mapping.

Fixtures are encrypted with the same primitives the client decrypts with, so
these exercise the real crypto path rather than a stand-in.
"""

import http.client
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'resources', 'lib'))

from Crypto.Cipher import AES  # noqa: E402

from updater import mega_client  # noqa: E402

TESTING_LINK = 'https://mega.nz/folder/mA0AAIRL#huzd7S1XCEYSQXPEcR_rHw'
# Must be the key the client derives from the link, or every node decrypts to
# garbage and the fixture silently proves nothing.
FOLDER_KEY = mega_client.parse_folder_link(TESTING_LINK)[1]

ROOT = 'ROOThnd1'


def encode_node_key(node_key):
    return mega_client.b64encode(AES.new(FOLDER_KEY, AES.MODE_ECB).encrypt(node_key))


def encode_attrs(name, aes_key):
    payload = b'MEGA' + json.dumps({'n': name}).encode('utf-8')
    payload += b'\0' * (-len(payload) % 16)
    return mega_client.b64encode(
        AES.new(aes_key, AES.MODE_CBC, b'\0' * 16).encrypt(payload))


def file_node(handle, parent, name, size=100, ts=1000, seed=1, foreign_key=False):
    """A file node.

    Real folders contain nodes shared more than once, whose 'k' lists a pair
    per principal. foreign_key reproduces that: another share's pair is listed
    first, and only the root-keyed pair opens the node.
    """
    node_key = bytes(((seed + i) % 251) for i in range(32))
    aes_key, _nonce = mega_client.unpack_node_key(node_key)
    keys = f'{ROOT}:{encode_node_key(node_key)}'
    if foreign_key:
        # Decrypts to a well-formed but wrong 32-byte key, exactly like the
        # live node that exposed this: length checks cannot reject it.
        other = bytes(((seed + 90 + i) % 251) for i in range(32))
        keys = f'OTHERSHR:{encode_node_key(other)}/' + keys
    return {
        'h': handle, 'p': parent, 't': 0,
        'k': keys,
        'a': encode_attrs(name, aes_key),
        's': size, 'ts': ts,
        }


def folder_node(handle, parent, name, seed=200):
    node_key = bytes(((seed + i) % 251) for i in range(16))
    return {
        'h': handle, 'p': parent, 't': 1,
        'k': f'{ROOT}:{encode_node_key(node_key)}',
        'a': encode_attrs(name, node_key),
        's': 0, 'ts': 0,
        }


def listing():
    """Shaped like the real testing folder: builds at root, archive subfolders."""
    return {'f': [
        {'h': ROOT, 'p': '', 't': 2, 'k': '', 'a': '', 's': 0, 'ts': 0},
        file_node('bld00001', ROOT,
                  'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar',
                  size=266338304, ts=1784000000, seed=1),
        file_node('sha00001', ROOT,
                  'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar.sha256',
                  size=132, ts=1784000000, seed=40),
        file_node('bld00002', ROOT,
                  'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260723175001.tar',
                  size=266338304, ts=1783900000, seed=80),
        # Shared more than once, so another share's key is listed first. A live
        # build was silently dropped from the Testing channel because of this.
        file_node('bld00004', ROOT,
                  'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260722161036.tar',
                  size=266188800, ts=1783800000, seed=200, foreign_key=True),
        folder_node('fld00001', ROOT, 'T4b'),
        folder_node('fld00002', ROOT, 'test vids'),
        # Archived build one level down: must never be offered.
        file_node('bld00003', 'fld00001',
                  'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260101000000.tar',
                  size=266338304, ts=1700000000, seed=120),
        file_node('vid00001', 'fld00002', 'holiday.mp4', size=99, ts=1700000000, seed=160),
        ]}


class FakeOpener:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        body = json.dumps(payload).encode('utf-8') if not isinstance(payload, bytes) else payload
        return _FakeResponse(body)


class _FakeResponse(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.headers = {'Content-Length': str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TestFolderLink(unittest.TestCase):

    def test_parses_real_link(self):
        folder_id, key = mega_client.parse_folder_link(TESTING_LINK)
        self.assertEqual(folder_id, 'mA0AAIRL')
        self.assertEqual(len(key), 16)

    def test_uppercase_is_accepted(self):
        folder_id, _ = mega_client.parse_folder_link(
            'https://mega.nz/folder/UpPeRcAs#BBBBBBBBBBBBBBBBBBBBBB')
        self.assertEqual(folder_id, 'UpPeRcAs')

    def test_rejects_bad_links(self):
        bad = [
            'https://mega.nz/file/mA0AAIRL#huzd7S1XCEYSQXPEcR_rHw',   # file, not folder
            'https://mega.nz/folder/mA0AAIRL',                        # no key
            'https://mega.nz/folder/SHORT#huzd7S1XCEYSQXPEcR_rHw',    # id too short
            'https://mega.nz/folder/mA0AAIRL#tooshort',               # key too short
            'https://example.com/folder/mA0AAIRL#huzd7S1XCEYSQXPEcR_rHw',
            '',
            ]
        for link in bad:
            with self.assertRaises(ValueError, msg=link):
                mega_client.parse_folder_link(link)


class TestKeyDerivation(unittest.TestCase):

    def test_unpack_matches_reference(self):
        """mega.c:1226: aes_key = k[0:16] XOR k[16:32]; nonce = k[16:24]."""
        node_key = bytes(range(32))
        aes_key, nonce = mega_client.unpack_node_key(node_key)
        self.assertEqual(aes_key, bytes(a ^ b for a, b in zip(range(16), range(16, 32))))
        self.assertEqual(nonce, bytes(range(16, 24)))

    def test_rejects_wrong_length(self):
        for length in (0, 16, 31, 33):
            with self.assertRaises(ValueError):
                mega_client.unpack_node_key(b'\0' * length)

    def test_node_key_decrypts_as_ecb(self):
        node_key = bytes(((7 + i) % 251) for i in range(32))
        recovered = mega_client.decrypt_node_key(encode_node_key(node_key), FOLDER_KEY)
        self.assertEqual(recovered, node_key)

    def test_attributes_round_trip(self):
        node_key = bytes(((3 + i) % 251) for i in range(32))
        aes_key, _ = mega_client.unpack_node_key(node_key)
        attrs = mega_client.decrypt_attributes(encode_attrs('build.tar', aes_key), aes_key)
        self.assertEqual(attrs.get('n'), 'build.tar')

    def test_key_candidates_put_share_matches_first(self):
        field = 'OTHERSHR:aaa/{root}:bbb'.format(root=ROOT)
        self.assertEqual(mega_client._key_candidates(field, (ROOT,)), ['bbb', 'aaa'])

    def test_key_candidates_without_a_match_still_offers_every_key(self):
        """Better to try a key that may not work than to drop the node."""
        self.assertEqual(mega_client._key_candidates('A:aaa/B:bbb', ('ZZ',)),
                         ['aaa', 'bbb'])

    def test_key_candidates_ignores_malformed_parts(self):
        self.assertEqual(mega_client._key_candidates('junk/A:aaa', ()), ['aaa'])
        self.assertEqual(mega_client._key_candidates('', ()), [])
        self.assertEqual(mega_client._key_candidates(None, ()), [])

    def test_attributes_with_wrong_key_yield_nothing(self):
        node_key = bytes(((3 + i) % 251) for i in range(32))
        aes_key, _ = mega_client.unpack_node_key(node_key)
        blob = encode_attrs('build.tar', aes_key)
        self.assertEqual(mega_client.decrypt_attributes(blob, b'\1' * 16), {})


class TestKnownAnswerCrypto(unittest.TestCase):
    """One pinned vector, decrypted only - nothing here is round-tripped.

    Every other crypto test encrypts its fixture with the same primitives the
    client decrypts with, so a matched pair of mistakes would cancel out. The
    constants below are literals, so base64url, the ECB unwrap, the key fold
    and the CBC attributes are all pinned in one go: change any of them and
    this fails.

    Derived once from a dummy folder link, not captured from Mega - publishing
    a real folder link is exactly what the internal channel exists to avoid.
    Recompute with:

        folder_key = bytes(range(16))          # 'AAECAwQFBgcICQoLDA0ODw'
        node_key   = bytes(range(32))
        WRAPPED    = b64url(AES-128-ECB(folder_key).encrypt(node_key))
        ATTRIBUTES = b64url(AES-128-CBC(aes_key, IV=0).encrypt(
                         b'MEGA' + json + zero padding to 16))
    """

    LINK = 'https://mega.nz/folder/KNOWNANS#AAECAwQFBgcICQoLDA0ODw'
    FOLDER_KEY = bytes(range(16))
    NODE_KEY = bytes(range(32))
    WRAPPED_NODE_KEY = 'CpQLtUFu8EXxw5RYxlPqWgf-73Th1QNukA7uEY6UkpM'
    # k[0:16] XOR k[16:32], which for bytes(range(32)) is 0x10 in every byte.
    AES_KEY = bytes.fromhex('10101010101010101010101010101010')
    NONCE = bytes.fromhex('1011121314151617')
    ATTRIBUTES = ('LJihjnklnWSAGrUMk9llzvtC77NI3Et3hzcbBL8JuyF732VqhE9x55ClYKD8'
                  'hML4V7Zz3QoUvX8hHKxQOtp9AZ7O6xl2NtKkzwA7ZiQcpEw')
    NAME = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar'
    # AES-CTR over the same key and nonce, for 'p3i known-answer payload block!!'.
    CIPHERTEXT = '7bGxSJxOOyNS-Mvc9GVY0gvHIwlcgN8-tRulOs3BUqQ'
    PLAINTEXT = b'p3i known-answer payload block!!'

    def test_the_link_yields_the_folder_key(self):
        folder_id, key = mega_client.parse_folder_link(self.LINK)
        self.assertEqual(folder_id, 'KNOWNANS')
        self.assertEqual(key, self.FOLDER_KEY)

    def test_the_wrapped_node_key_unwraps_under_the_folder_key(self):
        self.assertEqual(
            mega_client.decrypt_node_key(self.WRAPPED_NODE_KEY, self.FOLDER_KEY),
            self.NODE_KEY)

    def test_the_node_key_folds_to_the_expected_aes_key_and_nonce(self):
        aes_key, nonce = mega_client.unpack_node_key(self.NODE_KEY)
        self.assertEqual(aes_key, self.AES_KEY)
        self.assertEqual(nonce, self.NONCE)

    def test_the_attributes_decrypt_to_the_filename(self):
        attrs = mega_client.decrypt_attributes(self.ATTRIBUTES, self.AES_KEY)
        self.assertEqual(attrs.get('n'), self.NAME)

    def test_the_payload_stream_decrypts_under_the_folded_key(self):
        stream = mega_client.MegaDownload(
            _FakeResponse(mega_client.b64decode(self.CIPHERTEXT)),
            self.AES_KEY, self.NONCE, len(self.PLAINTEXT))
        self.assertEqual(stream.read(4096), self.PLAINTEXT)

    def test_the_whole_path_runs_end_to_end_from_a_raw_node(self):
        """Wrapped key in, filename out, through the client's own listing."""
        raw = {'f': [
            {'h': 'KATROOT1', 'p': '', 't': 2, 'k': '', 'a': '', 's': 0, 'ts': 0},
            {'h': 'katfile1', 'p': 'KATROOT1', 't': 0,
             'k': 'KATROOT1:' + self.WRAPPED_NODE_KEY,
             'a': self.ATTRIBUTES, 's': 266338304, 'ts': 1784000000},
            ]}
        folder = mega_client.MegaFolder(
            self.LINK, opener=FakeOpener([[raw]]), sleep=lambda _s: None)
        files = folder.list_root_files()
        self.assertEqual([f.name for f in files], [self.NAME])
        self.assertEqual(files[0].aes_key, self.AES_KEY)
        self.assertEqual(files[0].nonce, self.NONCE)


class TestListing(unittest.TestCase):

    def folder(self, responses):
        return mega_client.MegaFolder(
            TESTING_LINK, opener=FakeOpener(responses), sleep=lambda _s: None)

    def test_sends_folder_id_but_never_the_key(self):
        opener = FakeOpener([[listing()]])
        folder = mega_client.MegaFolder(TESTING_LINK, opener=opener, sleep=lambda _s: None)
        folder.list_nodes()
        url = opener.requests[0].full_url
        self.assertIn('n=mA0AAIRL', url)
        self.assertNotIn('huzd7S1XCEYSQXPEcR_rHw', url)

    def test_sends_recursive_list_command(self):
        opener = FakeOpener([[listing()]])
        folder = mega_client.MegaFolder(TESTING_LINK, opener=opener, sleep=lambda _s: None)
        folder.list_nodes()
        payload = json.loads(opener.requests[0].data.decode())
        self.assertEqual(payload, [{'a': 'f', 'c': 1, 'r': 1}])

    def test_root_only_excludes_subfolder_contents(self):
        """The recursive listing returns archived builds; they must not appear."""
        files = self.folder([[listing()]]).list_root_files()
        names = sorted(f.name for f in files)
        self.assertEqual(names, [
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260722161036.tar',
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260723175001.tar',
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar',
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar.sha256',
            ])

    def test_a_node_shared_twice_is_not_lost(self):
        """Regression: a build vanished from Testing because 'k' listed another
        share's key first, and that key decrypts to a valid-looking 32 bytes."""
        files = self.folder([[listing()]]).list_root_files()
        names = [f.name for f in files]
        self.assertIn(
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260722161036.tar', names)
        self.assertEqual([n for n in names if not n], [], 'no unnamed nodes')

    def test_a_node_shared_twice_still_decrypts_its_payload_key(self):
        files = self.folder([[listing()]]).list_root_files()
        node = [f for f in files if f.name.endswith('20260722161036.tar')][0]
        self.assertEqual(len(node.aes_key), 16)
        self.assertEqual(len(node.nonce), 8)

    def test_root_only_excludes_folders_themselves(self):
        files = self.folder([[listing()]]).list_root_files()
        self.assertNotIn('T4b', [f.name for f in files])
        self.assertNotIn('test vids', [f.name for f in files])

    def test_file_nodes_carry_usable_keys(self):
        files = self.folder([[listing()]]).list_root_files()
        for node in files:
            self.assertEqual(len(node.aes_key), 16)
            self.assertEqual(len(node.nonce), 8)

    def test_rsa_wrapped_nodes_are_skipped(self):
        data = listing()
        data['f'][1]['k'] = 'bld00001:' + ('A' * 50)      # >= 46 chars: RSA
        files = self.folder([[data]]).list_root_files()
        self.assertNotIn('bld00001', [f.handle for f in files])

    def test_empty_listing(self):
        self.assertEqual(self.folder([[{'f': []}]]).list_root_files(), [])


class TestOneBadNodeDoesNotHideTheFolder(unittest.TestCase):
    """A node we cannot read is skipped; it must not take the listing with it.

    Mega serves whatever is in the folder, including nodes put there by other
    clients and nodes whose attributes we have no business reading. Aborting on
    the first of those empties the channel: the picker says there are no builds
    and the poll finds nothing, while the builds are sitting right there.
    """

    GOOD = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar'

    def folder(self, responses):
        return mega_client.MegaFolder(
            TESTING_LINK, opener=FakeOpener(responses), sleep=lambda _s: None)

    def listing_with(self, attributes):
        data = listing()
        node = dict(data['f'][1])
        node['h'] = 'oddball1'
        if attributes is None:
            node.pop('a', None)
        else:
            node['a'] = attributes
        data['f'].append(node)
        return data

    def names(self, attributes):
        return [f.name for f in self.folder([[self.listing_with(attributes)]])
                .list_root_files()]

    def test_a_node_with_no_attributes_at_all(self):
        self.assertIn(self.GOOD, self.names(None))

    def test_a_node_whose_attributes_are_empty(self):
        self.assertIn(self.GOOD, self.names(''))

    def test_a_node_whose_attributes_are_not_a_string(self):
        self.assertIn(self.GOOD, self.names(12345))

    def test_a_node_whose_attributes_are_not_base64(self):
        self.assertIn(self.GOOD, self.names('!!! not base64 !!!'))

    def test_a_node_whose_attributes_are_the_wrong_length(self):
        self.assertIn(self.GOOD, self.names('AAAA'))

    def test_the_unreadable_node_is_dropped_rather_than_named_blank(self):
        files = self.folder([[self.listing_with(None)]]).list_root_files()
        self.assertNotIn('oddball1', [f.handle for f in files])
        self.assertEqual([f for f in files if not f.name], [])


class TestDownloadUrl(unittest.TestCase):

    def test_requests_node_inside_folder_over_https(self):
        opener = FakeOpener([[listing()], [{'g': 'https://gfs.example/x', 's': 266338304}]])
        folder = mega_client.MegaFolder(TESTING_LINK, opener=opener, sleep=lambda _s: None)
        node = [n for n in folder.list_root_files() if n.name.endswith('.tar')][0]
        url, size = folder.resolve(node)
        payload = json.loads(opener.requests[1].data.decode())
        self.assertEqual(payload[0]['a'], 'g')
        self.assertEqual(payload[0]['n'], node.handle)   # 'n:' not 'p:'
        self.assertEqual(payload[0]['ssl'], 2)           # not megatools' ssl:0
        self.assertEqual(url, 'https://gfs.example/x')
        self.assertEqual(size, 266338304)


class TestStreamDecryption(unittest.TestCase):

    def test_ctr_stream_round_trips_across_chunk_boundaries(self):
        node_key = bytes(((11 + i) % 251) for i in range(32))
        aes_key, nonce = mega_client.unpack_node_key(node_key)
        plain = bytes((i % 256) for i in range(100000))
        ciphertext = AES.new(aes_key, AES.MODE_CTR, nonce=nonce).encrypt(plain)

        download = mega_client.MegaDownload(
            _FakeResponse(ciphertext), aes_key, nonce, len(ciphertext))
        out = b''
        while True:
            chunk = download.read(32768)
            if not chunk:
                break
            out += chunk
        self.assertEqual(out, plain)


class TestApiErrors(unittest.TestCase):

    def folder(self, responses):
        return mega_client.MegaFolder(
            TESTING_LINK, opener=FakeOpener(responses), sleep=lambda _s: None)

    def test_bare_negative_integer_becomes_an_error(self):
        with self.assertRaises(mega_client.MegaError) as ctx:
            self.folder([-9]).list_nodes()
        self.assertEqual(ctx.exception.code, -9)
        self.assertIn('not found', str(ctx.exception))

    def test_quota_error_is_reported_clearly(self):
        with self.assertRaises(mega_client.MegaError) as ctx:
            self.folder([[-17]]).list_nodes()
        self.assertIn('bandwidth', str(ctx.exception))
        self.assertFalse(ctx.exception.retryable)

    def test_access_denied(self):
        with self.assertRaises(mega_client.MegaError) as ctx:
            self.folder([[-11]]).list_nodes()
        self.assertIn('no longer valid', str(ctx.exception))

    def test_retryable_error_is_retried_then_succeeds(self):
        folder = self.folder([[-3], [listing()]])
        self.assertEqual(len(folder.list_root_files()), 4)

    def test_retryable_error_eventually_gives_up(self):
        with self.assertRaises(mega_client.MegaError) as ctx:
            self.folder([[-3]] * 8).list_nodes()
        self.assertEqual(ctx.exception.code, -3)

    def test_terminal_error_is_not_retried(self):
        opener = FakeOpener([[-16], [listing()]])
        folder = mega_client.MegaFolder(TESTING_LINK, opener=opener, sleep=lambda _s: None)
        with self.assertRaises(mega_client.MegaError):
            folder.list_nodes()
        self.assertEqual(len(opener.requests), 1)

    def test_the_codes_match_the_published_numbering(self):
        """Mega's own list: -12 EEXIST, -13 EINCOMPLETE, -14 EKEY, -15 ESID.

        Getting one wrong shows the user a confident sentence about the wrong
        failure, which is worse than 'error -13'.
        """
        self.assertEqual(mega_client.API_ERRORS[-12], 'already exists')
        self.assertIn('incomplete', mega_client.API_ERRORS[-13])
        self.assertIn('session', mega_client.API_ERRORS[-15])


class TestGivingUpIsPrompt(unittest.TestCase):
    """The last attempt is the last attempt.

    Kodi shuts the service down by asking the poll thread to stop; every second
    spent asleep after the outcome is already known is a second Kodi waits.
    """

    def sleeping_folder(self, responses):
        self.sleeps = []
        return mega_client.MegaFolder(
            TESTING_LINK, opener=FakeOpener(responses), sleep=self.sleeps.append)

    def test_no_backoff_is_served_after_the_final_attempt(self):
        folder = self.sleeping_folder([[-3]] * mega_client._MAX_ATTEMPTS)
        with self.assertRaises(mega_client.MegaError):
            folder.list_nodes()
        self.assertEqual(len(self.sleeps), mega_client._MAX_ATTEMPTS - 1,
                         'slept once more than it backed off between attempts')

    def test_the_backoff_still_doubles_between_attempts(self):
        folder = self.sleeping_folder([[-3]] * mega_client._MAX_ATTEMPTS)
        with self.assertRaises(mega_client.MegaError):
            folder.list_nodes()
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

    def test_a_terminal_error_never_sleeps(self):
        folder = self.sleeping_folder([[-16]])
        with self.assertRaises(mega_client.MegaError):
            folder.list_nodes()
        self.assertEqual(self.sleeps, [])


class TestTransportErrors(unittest.TestCase):
    """A dropped connection is as retryable as the API saying 'try again'.

    These are the failures a box on domestic wifi actually hits during a poll,
    and none of them arrive as a Mega error code.
    """

    def folder(self, responses):
        return mega_client.MegaFolder(
            TESTING_LINK, opener=FakeOpener(responses), sleep=lambda _s: None)

    def test_a_read_timeout_is_retried(self):
        """socket.timeout is TimeoutError, not a URLError, since 3.10."""
        folder = self.folder([TimeoutError('timed out'), [listing()]])
        self.assertEqual(len(folder.list_root_files()), 4)

    def test_a_dropped_connection_is_retried(self):
        folder = self.folder([http.client.RemoteDisconnected('closed'), [listing()]])
        self.assertEqual(len(folder.list_root_files()), 4)

    def test_a_truncated_body_is_retried(self):
        """A cut-off response decodes as invalid JSON, not as an error code."""
        folder = self.folder([b'{"f": [', [listing()]])
        self.assertEqual(len(folder.list_root_files()), 4)

    def test_a_name_resolution_failure_is_retried(self):
        folder = self.folder([urllib.error.URLError('temporary failure'), [listing()]])
        self.assertEqual(len(folder.list_root_files()), 4)

    def test_retrying_means_reissuing_the_request(self):
        opener = FakeOpener([TimeoutError('timed out')] * 2 + [[listing()]])
        folder = mega_client.MegaFolder(TESTING_LINK, opener=opener, sleep=lambda _s: None)
        folder.list_nodes()
        self.assertEqual(len(opener.requests), 3)

    def test_giving_up_reports_the_underlying_reason(self):
        with self.assertRaises(mega_client.MegaError) as ctx:
            self.folder([TimeoutError('timed out')] * 8).list_nodes()
        self.assertIn('timed out', str(ctx.exception))

    def test_giving_up_takes_the_full_attempt_budget(self):
        opener = FakeOpener([TimeoutError('timed out')] * 8)
        folder = mega_client.MegaFolder(TESTING_LINK, opener=opener, sleep=lambda _s: None)
        with self.assertRaises(mega_client.MegaError):
            folder.list_nodes()
        self.assertEqual(len(opener.requests), mega_client._MAX_ATTEMPTS)


if __name__ == '__main__':
    unittest.main()
