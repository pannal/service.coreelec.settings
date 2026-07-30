# SPDX-License-Identifier: GPL-2.0-or-later
"""The internal-channel gate and the stored-channel fallback."""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'resources', 'lib'))

from updater import channels  # noqa: E402

LINK = 'https://mega.nz/folder/EXAMPLE1#AAAAAAAAAAAAAAAAAAAAAA'
PASSWORD = 'correct horse battery staple'

# scrypt at N=2**14 is deliberately slow, so seal once and reuse.
SEALED = None


def setUpModule():
    global SEALED
    SEALED = channels.seal(LINK, PASSWORD)


class TestSeal(unittest.TestCase):

    def test_round_trip(self):
        self.assertEqual(channels.unseal(SEALED, PASSWORD), LINK)

    def test_blob_does_not_contain_the_link(self):
        """The whole point: the shipped artefact must not leak the folder."""
        self.assertNotIn('mega.nz', SEALED)
        self.assertNotIn('EXAMPLE1', SEALED)
        self.assertNotIn('EXAMPLE1', base64.b64decode(SEALED).decode('latin-1'))

    def test_wrong_password_rejected(self):
        with self.assertRaises(channels.WrongPassword):
            channels.unseal(SEALED, PASSWORD + '!')

    def test_empty_password_rejected(self):
        with self.assertRaises(channels.WrongPassword):
            channels.unseal(SEALED, '')

    def test_salt_makes_each_seal_distinct(self):
        other = channels.seal(LINK, PASSWORD)
        self.assertNotEqual(other, SEALED)
        self.assertEqual(channels.unseal(other, PASSWORD), LINK)

    def test_deterministic_with_fixed_salt_and_nonce(self):
        args = dict(salt=b'S' * 16, nonce=b'N' * 12)
        self.assertEqual(channels.seal(LINK, PASSWORD, **args),
                         channels.seal(LINK, PASSWORD, **args))


class TestSealFailureModes(unittest.TestCase):

    def test_missing_blob_is_not_provisioned(self):
        with self.assertRaises(channels.NotProvisioned):
            channels.unseal('', PASSWORD)

    def test_truncated_blob(self):
        with self.assertRaises(channels.WrongPassword):
            channels.unseal(SEALED[:20], PASSWORD)

    def test_not_base64(self):
        with self.assertRaises(channels.WrongPassword):
            channels.unseal('!!!not base64!!!', PASSWORD)

    def test_unknown_version_byte(self):
        raw = bytearray(base64.b64decode(SEALED))
        raw[0] = 99
        with self.assertRaises(channels.WrongPassword):
            channels.unseal(base64.b64encode(bytes(raw)).decode(), PASSWORD)

    def test_tampered_ciphertext_fails_authentication(self):
        """GCM's tag is the integrity check, not just the password check."""
        raw = bytearray(base64.b64decode(SEALED))
        raw[-20] ^= 0x01
        with self.assertRaises(channels.WrongPassword):
            channels.unseal(base64.b64encode(bytes(raw)).decode(), PASSWORD)


class TestChannelResolution(unittest.TestCase):

    def test_known_channels_pass_through(self):
        for name in channels.CHANNELS:
            self.assertEqual(channels.resolve_channel(name), name)

    def test_legacy_coreelec_train_falls_back(self):
        """Existing installs hold a CoreELEC train name here."""
        self.assertEqual(channels.resolve_channel('21-ng'), channels.RELEASE)

    def test_garbage_falls_back(self):
        for value in ('', None, 'nonsense', '9.2-ne', 'release'):
            self.assertEqual(channels.resolve_channel(value), channels.RELEASE)


class TestProvisioning(unittest.TestCase):

    def test_public_channels_always_provisioned(self):
        self.assertTrue(channels.is_provisioned(channels.RELEASE))
        self.assertTrue(channels.is_provisioned(channels.TESTING))

    def test_internal_requires_a_blob(self):
        original = channels.INTERNAL_BLOB
        try:
            channels.INTERNAL_BLOB = ''
            self.assertFalse(channels.is_provisioned(channels.INTERNAL))
            channels.INTERNAL_BLOB = SEALED
            self.assertTrue(channels.is_provisioned(channels.INTERNAL))
        finally:
            channels.INTERNAL_BLOB = original

    def test_shipped_blob_is_well_formed_and_leaks_nothing(self):
        """The shipped blob must parse and must not carry the link in the clear.

        The password itself is deliberately absent from this repository, so no
        test can assert the blob actually opens - that is what the on-box unlock
        check is for.
        """
        blob = channels.INTERNAL_BLOB
        if not blob:
            self.skipTest('internal channel not provisioned')

        raw = base64.b64decode(blob)
        self.assertEqual(raw[0], channels.BLOB_VERSION)
        self.assertGreater(len(raw), 1 + 16 + 12 + 16)

        for marker in ('mega.nz', 'folder', 'https'):
            self.assertNotIn(marker, blob)
            self.assertNotIn(marker, raw.decode('latin-1'))

    def test_shipped_blob_does_not_open_with_a_throwaway_password(self):
        """Guards against shipping a placeholder seal.

        The real password is deliberately absent from this repository, so no
        test can assert the blob opens. It can assert the blob does not open
        with the passwords a placeholder would have used, which is the mistake
        actually worth catching before a release.
        """
        blob = channels.INTERNAL_BLOB
        if not blob:
            self.skipTest('internal channel not provisioned')

        for candidate in ('test', 'testing', 'password', 'changeme', '1234', ''):
            with self.assertRaises(
                    (channels.WrongPassword, channels.NotProvisioned),
                    msg=f'the shipped blob opens with {candidate!r}'):
                channels.unseal(blob, candidate)


if __name__ == '__main__':
    unittest.main()
