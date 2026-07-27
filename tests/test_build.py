# SPDX-License-Identifier: GPL-2.0-or-later
"""Build identity: filtering, ordering, and the tag-parsing traps."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'resources', 'lib'))

from updater import build as build_mod  # noqa: E402

# Real names, copied from the three sources rather than invented.
GITHUB_TAR = 'Update_CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260704001258.tar'
GITHUB_SHA = GITHUB_TAR + '.sha256'
GITHUB_IMG = 'Flash_CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260704001258-Generic.img.gz'
GITHUB_IMG_SHA = GITHUB_IMG + '.sha256'
MEGA_TAR = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar'
MEGA_SHA = MEGA_TAR + '.sha256'
LOCAL_TAR = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_t3c-dev_20260724030150.tar'

RUNNING_VERSION = '21.3-Omega_p3i_T4b_20260719042311'


class TestIsBuild(unittest.TestCase):

    def test_accepts_tars_from_every_source(self):
        for name in (GITHUB_TAR, MEGA_TAR, LOCAL_TAR):
            self.assertTrue(build_mod.is_build(name), name)

    def test_rejects_everything_else(self):
        rejected = [
            GITHUB_SHA,
            MEGA_SHA,
            GITHUB_IMG,
            GITHUB_IMG_SHA,
            'CoreELEC-21.3.tar.gz',          # GitHub 'Source code (tar.gz)'
            'CoreELEC-21.3.zip',             # GitHub 'Source code (zip)'
            'holiday.mp4',                   # 'test vids' in the Mega folder
            'screenshot.png',
            '',
            ]
        for name in rejected:
            self.assertFalse(build_mod.is_build(name), name)


class TestTimestamp(unittest.TestCase):

    def test_from_filenames(self):
        self.assertEqual(build_mod.parse_timestamp(GITHUB_TAR), 20260704001258)
        self.assertEqual(build_mod.parse_timestamp(MEGA_TAR), 20260724161307)
        self.assertEqual(build_mod.parse_timestamp(LOCAL_TAR), 20260724030150)

    def test_from_os_release_version(self):
        """The running version has no 'Update_' prefix and no '.tar' suffix."""
        self.assertEqual(build_mod.parse_timestamp(RUNNING_VERSION), 20260719042311)

    def test_missing_stamp(self):
        self.assertIsNone(build_mod.parse_timestamp('CoreELEC-nightly.tar'))
        self.assertIsNone(build_mod.parse_timestamp(''))

    def test_short_digit_run_is_not_a_stamp(self):
        self.assertIsNone(build_mod.parse_timestamp('CoreELEC_2026072416130.tar'))


class TestTag(unittest.TestCase):

    def test_plain_tag(self):
        self.assertEqual(build_mod.parse_tag(GITHUB_TAR), 'T4b')

    def test_tag_containing_underscore(self):
        """'T4c_dev' is why name.split('_') indexing is banned."""
        self.assertEqual(build_mod.parse_tag(MEGA_TAR), 'T4c_dev')

    def test_tag_containing_hyphen(self):
        self.assertEqual(build_mod.parse_tag(LOCAL_TAR), 't3c-dev')

    def test_unparseable_tag_is_empty_not_an_error(self):
        self.assertEqual(build_mod.parse_tag('something-else_20260724161307.tar'), '')

    def test_respects_builder_name(self):
        name = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_other_X1_20260724161307.tar'
        self.assertEqual(build_mod.parse_tag(name, builder='other'), 'X1')
        self.assertEqual(build_mod.parse_tag(name, builder='p3i'), '')


class TestMakeBuild(unittest.TestCase):

    def test_non_build_returns_none(self):
        self.assertIsNone(build_mod.make_build(GITHUB_SHA, ref='x'))

    def test_populates_fields(self):
        entry = build_mod.make_build(MEGA_TAR, ref='url', size=266338304)
        self.assertEqual(entry.timestamp, 20260724161307)
        self.assertEqual(entry.tag, 'T4c_dev')
        self.assertEqual(entry.ref, 'url')

    def test_falls_back_to_upload_time_only_without_a_filename_stamp(self):
        entry = build_mod.make_build(
            'CoreELEC-nightly.tar', ref='u', fallback_timestamp=20200101000000)
        self.assertEqual(entry.timestamp, 20200101000000)

    def test_filename_stamp_beats_upload_time(self):
        """A re-uploaded old build must not claim to be newest."""
        entry = build_mod.make_build(
            GITHUB_TAR, ref='u', fallback_timestamp=20991231235959)
        self.assertEqual(entry.timestamp, 20260704001258)

    def test_no_stamp_anywhere_is_dropped(self):
        self.assertIsNone(build_mod.make_build('CoreELEC-nightly.tar', ref='u'))


class TestOrdering(unittest.TestCase):

    def setUp(self):
        self.builds = [
            build_mod.make_build(GITHUB_TAR, ref='a'),
            build_mod.make_build(MEGA_TAR, ref='b'),
            build_mod.make_build(LOCAL_TAR, ref='c'),
            ]

    def test_newest_first(self):
        ordered = build_mod.sort_builds(self.builds)
        self.assertEqual([b.timestamp for b in ordered],
                         [20260724161307, 20260724030150, 20260704001258])

    def test_newest_helper(self):
        self.assertEqual(build_mod.newest(self.builds).timestamp, 20260724161307)

    def test_newest_of_nothing(self):
        self.assertIsNone(build_mod.newest([]))

    def test_ordering_ignores_tag(self):
        """Channel is the track; crossing T4b -> T4c_dev is expected."""
        ordered = build_mod.sort_builds(self.builds)
        self.assertEqual(ordered[0].tag, 'T4c_dev')


class TestRunningBuild(unittest.TestCase):

    def setUp(self):
        self.running = build_mod.make_build(
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260719042311.tar', ref='r')
        self.builds = [build_mod.make_build(MEGA_TAR, ref='b'), self.running]

    def test_finds_running_across_naming_differences(self):
        found = build_mod.find_running(self.builds, RUNNING_VERSION)
        self.assertIsNotNone(found)
        self.assertEqual(found.timestamp, 20260719042311)

    def test_running_absent_from_channel(self):
        self.assertIsNone(build_mod.find_running(
            [build_mod.make_build(MEGA_TAR, ref='b')], RUNNING_VERSION))

    def test_newer_detection(self):
        newer = build_mod.make_build(MEGA_TAR, ref='b')
        self.assertTrue(build_mod.is_newer_than_running(newer, RUNNING_VERSION))
        self.assertFalse(build_mod.is_newer_than_running(self.running, RUNNING_VERSION))

    def test_older_build_is_not_newer(self):
        older = build_mod.make_build(GITHUB_TAR, ref='a')
        self.assertFalse(build_mod.is_newer_than_running(older, RUNNING_VERSION))

    def test_unparseable_running_version_claims_nothing(self):
        newer = build_mod.make_build(MEGA_TAR, ref='b')
        self.assertFalse(build_mod.is_newer_than_running(newer, 'garbage'))
        self.assertFalse(build_mod.is_newer_than_running(newer, ''))


class TestLabels(unittest.TestCase):

    def test_stamp_formatting(self):
        self.assertEqual(build_mod.format_stamp(20260724161307), '2026-07-24 16:13')

    def test_label_shows_tag_date_and_size(self):
        entry = build_mod.make_build(MEGA_TAR, ref='x', size=266338304)
        self.assertEqual(entry.label(), 'T4c_dev   2026-07-24 16:13   254.0 MB')

    def test_size_matches_what_github_and_mega_display(self):
        """Both web UIs compute in mebibytes and label it 'MB'; so must we, or
        the picker contradicts the page the build came from."""
        self.assertEqual(build_mod.format_size(254 * 1024 * 1024), '254.0 MB')
        self.assertEqual(build_mod.format_size(0), '')

    def test_label_falls_back_to_filename_without_a_tag(self):
        entry = build_mod.make_build('weird_20260724161307.tar', ref='x')
        self.assertTrue(entry.label().startswith('weird_20260724161307.tar'))


class TestSerial(unittest.TestCase):
    """The last four digits of the build stamp.

    This is the number the developer quotes in Discord alongside the patch
    notes, so on the dev channels it is the field a tester actually matches on.
    """

    def test_serial_is_the_last_four_digits_of_the_stamp(self):
        entry = build_mod.make_build(
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_t3c-dev_20260727162111.tar', ref='x')
        self.assertEqual(entry.serial, '2111')

    def test_the_label_can_carry_the_serial_right_after_the_tag(self):
        entry = build_mod.make_build(
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_t3c-dev_20260727162111.tar',
            ref='x', size=266338304)
        self.assertEqual(entry.label(serial=True),
                         't3c-dev 2111   2026-07-27 16:21   254.0 MB')

    def test_the_label_leaves_the_serial_out_by_default(self):
        entry = build_mod.make_build(MEGA_TAR, ref='x', size=266338304)
        self.assertNotIn('1307', entry.label())

    def test_a_build_with_no_usable_stamp_has_no_serial(self):
        """A fallback timestamp is not the number quoted in Discord."""
        entry = build_mod.make_build('weird.tar', ref='x', fallback_timestamp=None)
        self.assertIsNone(entry)


if __name__ == '__main__':
    unittest.main()
