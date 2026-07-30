# SPDX-License-Identifier: GPL-2.0-or-later
"""Channel sources: asset selection, the prerelease matrix, sidecar pairing."""

import email.message
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'resources', 'lib'))

from updater import build as build_mod  # noqa: E402
from updater import providers  # noqa: E402

from test_mega_client import (  # noqa: E402
    TESTING_LINK, FakeOpener, listing)

TAR_T4B = 'Update_CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260704001258.tar'
TAR_T4A = 'Update_CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4a_20260629220632.tar'
IMG_T4B = 'Flash_CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260704001258-Generic.img.gz'


def asset(name, size=1000):
    return {
        'name': name,
        'size': size,
        'created_at': '2026-07-04T00:12:58Z',
        'browser_download_url': f'https://github.test/{name}',
        }


def release(tag, assets, draft=False, prerelease=False):
    return {'tag_name': tag, 'draft': draft, 'prerelease': prerelease, 'assets': assets}


# Mirrors the real release layout: installer image, update tar, both sidecars.
FULL_ASSETS = [
    asset(IMG_T4B, 236000000),
    asset(IMG_T4B + '.sha256', 145),
    asset(TAR_T4B, 266338304),
    asset(TAR_T4B + '.sha256', 135),
    ]


class GitHubHarness:
    def __init__(self, releases):
        self.opener = FakeOpener([releases])

    def provider(self, include_prereleases=False):
        return providers.GitHubReleasesProvider(
            'pannal/CoreELEC', include_prereleases=include_prereleases, opener=self.opener)


class TestSha256Parsing(unittest.TestCase):

    def test_sha256sum_format(self):
        digest = 'd344adbb33d28aa0e256d21307b390191d27cc3c8226f4c15e44e4151c0cef31'
        self.assertEqual(providers.parse_sha256(f'{digest}  {TAR_T4B}\n'), digest)

    def test_binary_marker_format(self):
        digest = 'a' * 64
        self.assertEqual(providers.parse_sha256(f'{digest} *{TAR_T4B}\n'), digest)

    def test_bare_digest(self):
        digest = 'b' * 64
        self.assertEqual(providers.parse_sha256(digest), digest)

    def test_accepts_bytes_and_normalises_case(self):
        digest = 'C' * 64
        self.assertEqual(providers.parse_sha256(digest.encode()), 'c' * 64)

    def test_rejects_garbage(self):
        for text in ('', 'not a digest', 'a' * 63, None):
            self.assertIsNone(providers.parse_sha256(text))


class TestIsoTimestamps(unittest.TestCase):

    def test_github_created_at(self):
        self.assertEqual(providers._stamp_from_iso('2026-07-04T00:12:58Z'), 20260704001258)

    def test_bad_input(self):
        self.assertIsNone(providers._stamp_from_iso('nonsense'))
        self.assertIsNone(providers._stamp_from_iso(''))


class TestGitHubProvider(unittest.TestCase):

    def test_selects_only_the_update_tar(self):
        builds = GitHubHarness([release('T4b', FULL_ASSETS)]).provider().list_builds()
        self.assertEqual([b.name for b in builds], [TAR_T4B])

    def test_pairs_the_sidecar(self):
        builds = GitHubHarness([release('T4b', FULL_ASSETS)]).provider().list_builds()
        self.assertEqual(builds[0].sha256_ref, f'https://github.test/{TAR_T4B}.sha256')

    def test_download_url_and_size(self):
        builds = GitHubHarness([release('T4b', FULL_ASSETS)]).provider().list_builds()
        self.assertEqual(builds[0].ref, f'https://github.test/{TAR_T4B}')
        self.assertEqual(builds[0].size, 266338304)

    def test_drafts_are_always_skipped(self):
        harness = GitHubHarness([release('T4b', FULL_ASSETS, draft=True)])
        self.assertEqual(harness.provider(include_prereleases=True).list_builds(), [])

    def test_prereleases_hidden_by_default(self):
        harness = GitHubHarness([release('T4b', FULL_ASSETS, prerelease=True)])
        self.assertEqual(harness.provider().list_builds(), [])

    def test_prereleases_shown_when_opted_in(self):
        harness = GitHubHarness([release('T4b', FULL_ASSETS, prerelease=True)])
        builds = harness.provider(include_prereleases=True).list_builds()
        self.assertEqual([b.name for b in builds], [TAR_T4B])

    def test_stable_releases_unaffected_by_the_toggle(self):
        """Every current p3i release is prerelease:false, so default-off is safe."""
        for opt_in in (False, True):
            harness = GitHubHarness([release('T4b', FULL_ASSETS)])
            self.assertEqual(len(harness.provider(include_prereleases=opt_in).list_builds()), 1)

    def test_orders_across_releases(self):
        harness = GitHubHarness([
            release('T4a', [asset(TAR_T4A)]),
            release('T4b', [asset(TAR_T4B)]),
            ])
        builds = harness.provider().list_builds()
        self.assertEqual([b.timestamp for b in builds], [20260704001258, 20260629220632])

    def test_release_without_a_sidecar_still_lists(self):
        harness = GitHubHarness([release('T4b', [asset(TAR_T4B)])])
        builds = harness.provider().list_builds()
        self.assertEqual(len(builds), 1)
        self.assertIsNone(builds[0].sha256_ref)

    def test_empty_release_set(self):
        self.assertEqual(GitHubHarness([]).provider().list_builds(), [])

    def test_sends_a_user_agent(self):
        """GitHub rejects API requests without one."""
        harness = GitHubHarness([release('T4b', FULL_ASSETS)])
        harness.provider().list_builds()
        self.assertIn('User-agent', dict(harness.opener.requests[0].headers))


class TestMegaProvider(unittest.TestCase):

    def provider(self, responses):
        return providers.MegaFolderProvider(TESTING_LINK, opener=FakeOpener(responses))

    def test_lists_only_root_tars(self):
        builds = self.provider([[listing()]]).list_builds()
        self.assertEqual([b.name for b in builds], [
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar',
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260723175001.tar',
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260722161036.tar',
            ])

    def test_excludes_archived_builds_in_subfolders(self):
        builds = self.provider([[listing()]]).list_builds()
        self.assertNotIn(20260101000000, [b.timestamp for b in builds])

    def test_pairs_the_sidecar_node(self):
        builds = self.provider([[listing()]]).list_builds()
        newest = builds[0]
        self.assertIsNotNone(newest.sha256_ref)
        self.assertEqual(newest.sha256_ref.name, newest.name + '.sha256')

    def test_build_without_a_sidecar_has_none(self):
        builds = self.provider([[listing()]]).list_builds()
        self.assertIsNone(builds[1].sha256_ref)

    def test_newest_first(self):
        builds = self.provider([[listing()]]).list_builds()
        self.assertEqual(builds[0].timestamp, 20260724161307)

    def test_tags_parsed_for_display(self):
        builds = self.provider([[listing()]]).list_builds()
        self.assertEqual(builds[0].tag, 'T4c_dev')


class TestGitHubErrorPayloads(unittest.TestCase):
    """GitHub answers errors with an object, not the array of releases.

    Iterating it walks the key strings instead, and the user is shown an
    AttributeError where the API's own explanation was available.
    """

    def test_a_rate_limit_is_reported_in_its_own_words(self):
        harness = GitHubHarness({
            'message': 'API rate limit exceeded for 203.0.113.7.',
            'documentation_url': 'https://docs.github.com/rest',
            })
        with self.assertRaises(providers.ProviderError) as ctx:
            harness.provider().list_builds()
        self.assertIn('rate limit', str(ctx.exception))

    def test_a_missing_repository_is_reported(self):
        harness = GitHubHarness({'message': 'Not Found'})
        with self.assertRaises(providers.ProviderError) as ctx:
            harness.provider().list_builds()
        self.assertIn('Not Found', str(ctx.exception))

    def test_an_unexpected_shape_is_reported_not_iterated(self):
        for payload in ('nonsense', 42, None):
            harness = GitHubHarness(payload)
            with self.assertRaises(providers.ProviderError, msg=repr(payload)):
                harness.provider().list_builds()


def http_error(code, reason, body=b''):
    """What urlopen actually raises for a 4xx/5xx, body and all."""
    headers = email.message.Message()
    headers['Content-Type'] = 'application/json'
    return urllib.error.HTTPError(
        'https://api.github.test/repos/pannal/CoreELEC/releases',
        code, reason, headers, io.BytesIO(body))


class ErrorOpener:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        raise self.error


class TestGitHubErrorsArriveAsExceptions(unittest.TestCase):
    """The status, not the body, is how GitHub reports these.

    urlopen raises HTTPError for anything that is not a 2xx, so a rate limit or
    a missing repository never reaches the payload check above - the explanation
    GitHub put in the body is thrown past it and the user gets a repr() of a
    Python exception instead.
    """

    def provider(self, error):
        self.opener = ErrorOpener(error)
        return providers.GitHubReleasesProvider('pannal/CoreELEC', opener=self.opener)

    def test_a_rate_limit_is_reported_in_githubs_words(self):
        body = json.dumps({
            'message': 'API rate limit exceeded for 203.0.113.7.',
            'documentation_url': 'https://docs.github.com/rest',
            }).encode('utf-8')
        provider = self.provider(http_error(403, 'rate limit exceeded', body))
        with self.assertRaises(providers.ProviderError) as ctx:
            provider.list_builds()
        self.assertIn('rate limit exceeded for', str(ctx.exception))

    def test_a_missing_repository_is_reported_in_githubs_words(self):
        body = json.dumps({'message': 'Not Found'}).encode('utf-8')
        provider = self.provider(http_error(404, 'Not Found', body))
        with self.assertRaises(providers.ProviderError) as ctx:
            provider.list_builds()
        self.assertIn('Not Found', str(ctx.exception))

    def test_an_error_without_a_usable_body_still_names_the_status(self):
        provider = self.provider(http_error(500, 'Internal Server Error', b'<html>'))
        with self.assertRaises(providers.ProviderError) as ctx:
            provider.list_builds()
        self.assertIn('500', str(ctx.exception))

    def test_an_error_with_an_unreadable_body_does_not_mask_the_status(self):
        """e.read() is a socket read, and can fail like any other."""
        error = http_error(502, 'Bad Gateway')
        error.read = lambda *a: (_ for _ in ()).throw(OSError('connection reset'))
        provider = self.provider(error)
        with self.assertRaises(providers.ProviderError) as ctx:
            provider.list_builds()
        self.assertIn('502', str(ctx.exception))

    def test_the_sidecar_fetch_reports_the_same_way(self):
        provider = self.provider(http_error(404, 'Not Found',
                                            json.dumps({'message': 'Not Found'}).encode()))
        entry = build_mod.make_build(TAR_T4B, ref='x', size=1,
                                     sha256_ref='https://github.test/x.sha256')
        with self.assertRaises(providers.ProviderError):
            provider.fetch_sha256(entry)

    def test_a_transport_failure_is_left_alone(self):
        """Not an HTTP answer at all; nothing here can explain it better."""
        provider = self.provider(urllib.error.URLError('temporary failure'))
        with self.assertRaises(urllib.error.URLError):
            provider.list_builds()


class TestPickerLimit(unittest.TestCase):

    def test_limit_is_defined_and_sane(self):
        self.assertGreater(providers.PICKER_LIMIT, 0)
        self.assertLessEqual(providers.PICKER_LIMIT, 100)


if __name__ == '__main__':
    unittest.main()
