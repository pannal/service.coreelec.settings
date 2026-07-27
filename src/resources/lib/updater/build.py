# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)

"""Build identity: what counts as an installable build, and which one is newer.

Deliberately free of I/O so it can be tested on a dev host without Kodi.

Filenames differ between sources and the tag is not a stable token:

    GitHub  Update_CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260704001258.tar
    Mega           CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar
    local          CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_t3c-dev_20260724030150.tar

GitHub prefixes 'Update_' and Mega does not; the tag may itself contain '_'
('T4c_dev') or '-' ('t3c-dev'). Never index into name.split('_').
"""

import re
from dataclasses import dataclass, field
from typing import Any

# Ordering only, never filtering. Matches a build filename ('..._20260724161307.tar')
# and an os-release VERSION ('21.3-Omega_p3i_T4b_20260719042311') alike.
_TIMESTAMP_RE = re.compile(r'_(\d{14})(?:\.tar)?$')

BUILD_SUFFIX = '.tar'


def is_build(name):
    """True for an installable build artefact.

    The bare suffix is the whole filter, and it is sufficient: '.tar.sha256'
    sidecars, 'Flash_*.img.gz' installer images, GitHub's generated
    'Source code (tar.gz)'/'(zip)', and the test media sitting in the Mega
    testing folder all fail it.
    """
    return name.endswith(BUILD_SUFFIX)


def parse_timestamp(name):
    """The 14-digit build stamp, or None if the name does not carry one."""
    match = _TIMESTAMP_RE.search(name)
    return int(match.group(1)) if match else None


def parse_tag(name, builder='p3i'):
    """The opaque build tag between the builder name and the timestamp.

    'T4b', 'T4c_dev', 't3c-dev'. Display only - an unparseable name yields ''.
    """
    match = re.search(r'_' + re.escape(builder) + r'_(.+?)_\d{14}(?:\.tar)?$', name)
    return match.group(1) if match else ''


def format_size(num_bytes):
    """Size as both the GitHub and Mega web UIs render it.

    Both compute in mebibytes and label the result 'MB', so the picker must do
    the same or it shows a different number than the page the build came from.
    """
    if not num_bytes:
        return ''
    return f'{num_bytes / (1024 * 1024):.1f} MB'


def format_stamp(stamp):
    """20260724161307 -> '2026-07-24 16:13'."""
    text = str(stamp)
    if len(text) != 14:
        return text
    return f'{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}'


@dataclass
class Build:
    """One installable build, normalised across every channel source."""

    name: str
    timestamp: int
    tag: str = ''
    size: int = 0
    ref: Any = None            # str URL (GitHub) | MegaNode (Mega)
    sha256_ref: Any = None     # sidecar, same union; None if the source has none
    extra: dict = field(default_factory=dict)

    @property
    def serial(self):
        """The last four digits of the build stamp.

        This is the number the developer quotes in Discord alongside the patch
        notes, so on the dev channels it is the field a tester matches a build
        on. Empty for a stamp that is not a full 14-digit one.
        """
        text = str(self.timestamp)
        return text[-4:] if len(text) == 14 else ''

    def label(self, serial=False):
        """Picker row: 'T4c_dev   2026-07-24 16:13   253.9 MB'.

        serial appends the build's serial to the tag, for channels where that
        is how a build gets referred to in conversation.
        """
        head = self.tag or self.name
        if serial and self.serial:
            head = '%s %s' % (head, self.serial)
        parts = [head, format_stamp(self.timestamp)]
        size = format_size(self.size)
        if size:
            parts.append(size)
        return '   '.join(parts)


def make_build(name, ref, size=0, sha256_ref=None, fallback_timestamp=None, builder='p3i'):
    """Build from a source listing, or None if it is not an installable build.

    fallback_timestamp is the source's own upload time (Mega node 'ts', GitHub
    asset 'created_at'), used only when the filename carries no stamp. The
    filename wins because the two diverge exactly where it matters: re-uploading
    an old build to roll a channel back makes upload time claim 'newest' while
    the filename correctly reports it as old.
    """
    if not is_build(name):
        return None
    timestamp = parse_timestamp(name)
    if timestamp is None:
        if fallback_timestamp is None:
            return None
        timestamp = fallback_timestamp
    return Build(
        name=name,
        timestamp=timestamp,
        tag=parse_tag(name, builder),
        size=size,
        ref=ref,
        sha256_ref=sha256_ref,
        )


def sort_builds(builds):
    """Newest first."""
    return sorted(builds, key=lambda b: b.timestamp, reverse=True)


def newest(builds):
    ordered = sort_builds(builds)
    return ordered[0] if ordered else None


def find_running(builds, version):
    """The Build matching the running os-release VERSION, or None.

    Matched on the 14-digit stamp rather than the whole string, because the
    running VERSION has no 'Update_' prefix and no '.tar' suffix.
    """
    stamp = parse_timestamp(version or '')
    if stamp is None:
        return None
    for build in builds:
        if build.timestamp == stamp:
            return build
    return None


def is_newer_than_running(build, version):
    """True if build supersedes the running system.

    An unparseable running version means we cannot prove the build is newer, so
    we do not claim it is.
    """
    stamp = parse_timestamp(version or '')
    if stamp is None:
        return False
    return build.timestamp > stamp
