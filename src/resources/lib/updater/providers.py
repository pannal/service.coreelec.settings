# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)

"""Channel sources, normalised to one interface.

Every provider answers three questions and nothing else:

    list_builds()        which builds exist, newest first
    fetch_sha256(build)  the expected digest, from the sidecar
    open(build)          a reader with .read(n) and .size

That keeps ordering, selection, verification and staging written once in the
updates module, with the source-specific I/O confined here.
"""

import calendar
import json
import re
import time
import urllib.error
import urllib.request

from . import build as build_mod
from . import mega_client

_TIMEOUT = 30
_USER_AGENT = 'CoreELEC-settings-updater'
_SHA256_RE = re.compile(r'\b([0-9a-fA-F]{64})\b')

GITHUB_API = 'https://api.github.com/repos/{repo}/releases?per_page=100'

SHA256_SUFFIX = '.sha256'

# Most builds the picker will show. Keeps the Kodi select dialog scrollable as
# a channel accumulates history.
PICKER_LIMIT = 25


class ProviderError(Exception):
    """A channel source answered, but not with something we can use.

    Carries wording fit to put in front of a user, the way MegaError does, so
    the caller can show it instead of a repr().
    """


def _http_error_message(exc):
    """GitHub's own words for an HTTP error, or the bare status.

    The body is the only place the useful sentence lives ('API rate limit
    exceeded for <ip>'), and reading it is a socket read that can fail on its
    own, so the status is always available as a fallback.
    """
    try:
        payload = json.loads(exc.read().decode('utf-8', 'replace'))
    except Exception:
        payload = None
    message = payload.get('message') if isinstance(payload, dict) else None
    return message or 'HTTP %s from GitHub' % exc.code


def parse_sha256(text):
    """First 64-hex token of a sha256sum-format sidecar, lowercased."""
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    match = _SHA256_RE.search(text or '')
    return match.group(1).lower() if match else None


def _stamp_from_epoch(epoch):
    if not epoch:
        return None
    return int(time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)))


def _stamp_from_iso(text):
    """'2026-07-04T00:12:58Z' -> 20260704001258."""
    if not text:
        return None
    try:
        parsed = time.strptime(text, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return None
    return _stamp_from_epoch(calendar.timegm(parsed))


class _UrlReader:
    """Reader over a plain HTTPS response, matching MegaDownload's shape."""

    def __init__(self, response):
        self._response = response
        length = response.headers.get('Content-Length')
        self.size = int(length) if length and length.isdigit() else 0

    def read(self, amount):
        return self._response.read(amount)

    def close(self):
        try:
            self._response.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class GitHubReleasesProvider:
    """Release assets from a GitHub repository."""

    def __init__(self, repo, include_prereleases=False, opener=None, builder='p3i'):
        self.repo = repo
        self.include_prereleases = include_prereleases
        self.builder = builder
        self._opener = opener or urllib.request.urlopen

    def _get(self, url):
        request = urllib.request.Request(url, headers={
            'User-Agent': _USER_AGENT,
            'Accept': 'application/vnd.github+json',
            })
        try:
            with self._opener(request, timeout=_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # A rate limit or a missing repository is a 403/404, so urlopen
            # raises before the body is ever parsed. GitHub put its explanation
            # in that body; without this it is discarded and the user is shown
            # a repr() of the exception instead.
            raise ProviderError(_http_error_message(exc)) from exc

    @staticmethod
    def _releases(payload):
        """The release array, or the API's own words for why there isn't one.

        Rate limits and a missing repository come back as an object, not an
        array. Iterating that walks its keys instead, so every release looks
        like a string and the user is shown an AttributeError where GitHub had
        already explained itself.
        """
        if isinstance(payload, list):
            return payload
        message = payload.get('message') if isinstance(payload, dict) else None
        raise ProviderError(message or 'unexpected response from GitHub')

    def list_builds(self):
        payload = json.loads(self._get(GITHUB_API.format(repo=self.repo)).decode('utf-8'))
        builds = []
        for release in self._releases(payload):
            # Drafts are never publicly downloadable. Prereleases are the
            # user's call via the ShowPrereleases setting.
            if release.get('draft'):
                continue
            if release.get('prerelease') and not self.include_prereleases:
                continue

            assets = {a.get('name'): a for a in release.get('assets') or []}
            for name, asset in assets.items():
                if not build_mod.is_build(name):
                    continue
                sidecar = assets.get(name + SHA256_SUFFIX)
                entry = build_mod.make_build(
                    name=name,
                    ref=asset.get('browser_download_url'),
                    size=asset.get('size') or 0,
                    sha256_ref=(sidecar or {}).get('browser_download_url'),
                    fallback_timestamp=_stamp_from_iso(asset.get('created_at')),
                    builder=self.builder,
                    )
                if entry is not None:
                    builds.append(entry)
        return build_mod.sort_builds(builds)

    def fetch_sha256(self, entry):
        if not entry.sha256_ref:
            return None
        return parse_sha256(self._get(entry.sha256_ref))

    def open(self, entry):
        request = urllib.request.Request(entry.ref, headers={'User-Agent': _USER_AGENT})
        return _UrlReader(self._opener(request, timeout=_TIMEOUT))


class MegaFolderProvider:
    """Builds sitting in the root of a public Mega folder."""

    def __init__(self, link, opener=None, builder='p3i'):
        self.link = link
        self.builder = builder
        self._folder = mega_client.MegaFolder(link, opener=opener)

    def list_builds(self):
        nodes = self._folder.list_root_files()
        by_name = {n.name: n for n in nodes if n.name}
        builds = []
        for name, node in by_name.items():
            if not build_mod.is_build(name):
                continue
            entry = build_mod.make_build(
                name=name,
                ref=node,
                size=node.size,
                sha256_ref=by_name.get(name + SHA256_SUFFIX),
                fallback_timestamp=_stamp_from_epoch(node.timestamp),
                builder=self.builder,
                )
            if entry is not None:
                builds.append(entry)
        return build_mod.sort_builds(builds)

    def fetch_sha256(self, entry):
        if entry.sha256_ref is None:
            return None
        with self._folder.open(entry.sha256_ref) as stream:
            return parse_sha256(stream.read(4096))

    def open(self, entry):
        return self._folder.open(entry.ref)
