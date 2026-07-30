# SPDX-License-Identifier: GPL-2.0-or-later
"""The updates module's orchestration: channel routing, picker, install, polling.

Runs off-box against stubbed Kodi bindings. The fake oe.__() resolves through
the real strings.po, so referencing a string ID that was never added fails here
rather than showing up blank on a box.
"""

import glob
import hashlib
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path.insert(0, os.path.join(ROOT, 'src', 'resources', 'lib'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'resources', 'lib', 'modules'))

import kodi_stubs  # noqa: E402

kodi_stubs.install()

import updates as updates_mod  # noqa: E402
from updater import build as build_mod  # noqa: E402
from updater import channels as channels_mod  # noqa: E402
from updater import providers as providers_mod  # noqa: E402
from updater.mega_client import MegaError  # noqa: E402

RUNNING_VERSION = '21.3-Omega_p3i_T4b_20260719042311'

NEWEST = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260724161307.tar'
MIDDLE = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260723175001.tar'
RUNNING = 'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4b_20260719042311.tar'

INTERNAL_LINK = 'https://mega.nz/folder/EXAMPLE1#AAAAAAAAAAAAAAAAAAAAAA'


def load_strings():
    """Real msgctxt -> msgid map, so a missing string is a test failure."""
    path = os.path.join(ROOT, 'language', 'resource.language.en_gb', 'strings.po')
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    return {int(m.group(1)): m.group(2)
            for m in re.finditer(r'msgctxt "#(\d+)"\nmsgid "(.*?)"\n', text)}


def load_translations(path):
    """msgctxt -> msgstr for one strings.po, skipping untranslated entries."""
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    return {int(m.group(1)): m.group(3)
            for m in re.finditer(
                r'msgctxt "#(\d+)"\nmsgid "(.*?)"\nmsgstr "(.*?)"\n', text)
            if m.group(3)}


STRINGS = load_strings()


class FakeOE:
    LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3

    def __init__(self, temp_dir):
        self.TEMP = temp_dir + os.sep
        self.VERSION = RUNNING_VERSION
        self.BUILDER_NAME = 'p3i'
        self.ARCHITECTURE = 'Amlogic-ng.arm'
        self.settings = {}
        self.notifications = []
        self.logs = []
        self.ProgressDialog = kodi_stubs.FakeProgressDialog
        self.xbmcm = kodi_stubs.FakeMonitor()
        self.dictModules = {}

    def dbg_log(self, where, what, level=0):
        self.logs.append((where, what, level))

    def read_setting(self, module, setting, default=None):
        return self.settings.get((module, setting), default)

    def write_setting(self, module, setting, value, main_node='settings'):
        self.settings[(module, setting)] = value

    def _(self, code):
        if code not in STRINGS:
            raise KeyError(f'string #{code} is not defined in strings.po')
        return STRINGS[code]

    def notify(self, title, message, icon='icon'):
        self.notifications.append((title, message))

    def set_busy(self, state):
        pass


class FakeStream:
    def __init__(self, payload, size=None):
        self._buffer = payload
        self.size = size if size is not None else len(payload)
        self.closed = False

    def read(self, amount):
        chunk, self._buffer = self._buffer[:amount], self._buffer[amount:]
        return chunk

    def close(self):
        self.closed = True


class FakeProvider:
    def __init__(self, builds, payload=b'', digest='auto', open_error=None,
                 list_error=None):
        self.builds = builds
        self.payload = payload
        self._digest = digest
        self.open_error = open_error
        self.list_error = list_error
        self.streams = []

    def list_builds(self):
        if self.list_error:
            raise self.list_error
        return build_mod.sort_builds(self.builds)

    def fetch_sha256(self, entry):
        if self._digest == 'auto':
            return hashlib.sha256(self.payload).hexdigest()
        return self._digest

    def open(self, entry):
        if self.open_error:
            raise self.open_error
        stream = FakeStream(self.payload)
        self.streams.append(stream)
        return stream


def make_builds(*names):
    return [build_mod.make_build(name, ref=name, size=1000) for name in names]


class UpdatesTestCase(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset()
        self.temp = tempfile.mkdtemp()
        self.update_dir = os.path.join(self.temp, 'update')
        self.oe = FakeOE(self.temp)
        self.module = updates_mod.updates(self.oe)
        self.module.LOCAL_UPDATE_DIR = self.update_dir + os.sep
        self.addCleanup(shutil.rmtree, self.temp, True)

    def settings(self):
        return self.module.struct['update']['settings']

    def set_channel(self, name):
        self.settings()['Channel']['value'] = name


class TestLoadValues(UpdatesTestCase):

    def setUp(self):
        super().setUp()
        # Already migrated, so these exercise the ordinary read path rather
        # than the one-off reset, which would otherwise decide the channel for
        # them and let the assertions pass for the wrong reason.
        self.oe.settings[('updates', 'SettingsVersion')] = str(updates_mod.SETTINGS_VERSION)

    def test_legacy_coreelec_train_is_coerced(self):
        """Existing installs hold '21-ng' here, which matches no channel."""
        self.oe.settings[('updates', 'Channel')] = '21-ng'
        self.module.load_values()
        self.assertEqual(self.settings()['Channel']['value'], channels_mod.RELEASE)

    def test_known_channel_is_kept(self):
        self.oe.settings[('updates', 'Channel')] = channels_mod.TESTING
        self.module.load_values()
        self.assertEqual(self.settings()['Channel']['value'], channels_mod.TESTING)

    def test_missing_channel_defaults(self):
        self.module.load_values()
        self.assertEqual(self.settings()['Channel']['value'], channels_mod.RELEASE)

    def test_restores_persisted_settings(self):
        self.oe.settings[('updates', 'SettingsVersion')] = str(updates_mod.SETTINGS_VERSION)
        self.oe.settings[('updates', 'AutoUpdate')] = 'auto'
        self.oe.settings[('updates', 'UpdateNotify')] = '0'
        self.oe.settings[('updates', 'ShowPrereleases')] = '1'
        self.oe.settings[('updates', 'InternalLink')] = INTERNAL_LINK
        self.module.load_values()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'auto')
        self.assertEqual(self.settings()['UpdateNotify']['value'], '0')
        self.assertEqual(self.settings()['ShowPrereleases']['value'], '1')
        self.assertEqual(self.module.internal_link, INTERNAL_LINK)

    def test_detects_a_pending_staged_update(self):
        os.makedirs(self.update_dir)
        open(os.path.join(self.update_dir, 'SYSTEM'), 'w').close()
        self.module.load_values()
        self.assertTrue(hasattr(self.module, 'update_in_progress'))

    def test_a_staged_tar_survives_an_addon_restart(self):
        """What install() stages is a .tar; only that tells us one is pending.

        Missing it means the next poll re-downloads a quarter of a gigabyte and
        stages a second tar beside the first.
        """
        os.makedirs(self.update_dir)
        open(os.path.join(self.update_dir, NEWEST), 'w').close()
        self.module.load_values()
        self.assertTrue(hasattr(self.module, 'update_in_progress'))

    def test_an_empty_update_dir_is_not_pending(self):
        os.makedirs(self.update_dir)
        self.module.load_values()
        self.assertFalse(hasattr(self.module, 'update_in_progress'))

    def test_leftovers_that_are_not_builds_are_not_pending(self):
        """initramfs owns this directory and leaves its own files behind."""
        os.makedirs(self.update_dir)
        open(os.path.join(self.update_dir, '.nocompat'), 'w').close()
        self.module.load_values()
        self.assertFalse(hasattr(self.module, 'update_in_progress'))

    def test_a_directory_that_looks_like_a_build_is_not_pending(self):
        """Counting one would stop polling for good: it can never be cleared."""
        os.makedirs(os.path.join(self.update_dir, 'bogus.tar'))
        self.module.load_values()
        self.assertFalse(hasattr(self.module, 'update_in_progress'))


class TestSettingsMigration(UpdatesTestCase):
    """The old CoreELEC update service is gone and the channels are different.

    Nobody should have a p3i build downloaded and staged on their behalf
    because of a choice they made about the service this replaced, so the
    switch to the new updater turns auto-update off and announcements on, once.
    """

    def old_install(self, **settings):
        """A settings file written before the new updater existed."""
        for key, value in settings.items():
            self.oe.settings[('updates', key)] = value

    def test_an_existing_auto_updater_is_moved_to_manual(self):
        self.old_install(AutoUpdate='auto')
        self.module.load_values()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'manual')

    def test_notifications_are_switched_back_on(self):
        self.old_install(UpdateNotify='0')
        self.module.load_values()
        self.assertEqual(self.settings()['UpdateNotify']['value'], '1')

    def test_the_reset_is_persisted_not_just_held_in_memory(self):
        self.old_install(AutoUpdate='auto', UpdateNotify='0')
        self.module.load_values()
        self.assertEqual(self.oe.settings[('updates', 'AutoUpdate')], 'manual')
        self.assertEqual(self.oe.settings[('updates', 'UpdateNotify')], '1')

    def test_the_marker_is_written_so_it_does_not_repeat(self):
        self.module.load_values()
        self.assertEqual(self.oe.settings[('updates', 'SettingsVersion')],
                         str(updates_mod.SETTINGS_VERSION))

    def test_a_later_choice_of_auto_is_not_overruled(self):
        """The whole point of the marker: reset once, then leave them alone."""
        self.old_install(AutoUpdate='auto')
        self.module.load_values()

        # The user goes back into the menu and turns auto-update on again.
        self.oe.settings[('updates', 'AutoUpdate')] = 'auto'
        self.oe.settings[('updates', 'UpdateNotify')] = '0'
        fresh = updates_mod.updates(self.oe)
        fresh.LOCAL_UPDATE_DIR = self.update_dir + os.sep
        fresh.load_values()
        settings = fresh.struct['update']['settings']
        self.assertEqual(settings['AutoUpdate']['value'], 'auto')
        self.assertEqual(settings['UpdateNotify']['value'], '0')

    def test_a_fresh_install_lands_on_manual_with_notifications(self):
        self.module.load_values()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'manual')
        self.assertEqual(self.settings()['UpdateNotify']['value'], '1')

    def test_the_channel_is_reset_to_release(self):
        """A dev channel chosen under an earlier build is not carried over."""
        self.old_install(Channel=channels_mod.TESTING)
        self.module.load_values()
        self.assertEqual(self.settings()['Channel']['value'], channels_mod.RELEASE)
        self.assertEqual(self.oe.settings[('updates', 'Channel')], channels_mod.RELEASE)

    def test_the_internal_channel_is_reset_too(self):
        self.old_install(Channel=channels_mod.INTERNAL, InternalLink=INTERNAL_LINK)
        self.module.load_values()
        self.assertEqual(self.settings()['Channel']['value'], channels_mod.RELEASE)

    def test_the_migration_leaves_other_settings_alone(self):
        """An unlocked internal channel stays unlocked, and so on.

        The reset moves the box back to Release; it does not throw away work
        the user did to get at the other channels in the first place.
        """
        self.old_install(AutoUpdate='auto', ShowPrereleases='1',
                         Channel=channels_mod.TESTING, InternalLink=INTERNAL_LINK,
                         LastNotified='20260724161307')
        self.module.load_values()
        self.assertEqual(self.settings()['ShowPrereleases']['value'], '1')
        self.assertEqual(self.module.internal_link, INTERNAL_LINK)
        self.assertEqual(self.module.last_notified, 20260724161307)

    def test_a_later_choice_of_channel_is_not_overruled(self):
        self.old_install(Channel=channels_mod.TESTING)
        self.module.load_values()

        self.oe.settings[('updates', 'Channel')] = channels_mod.TESTING
        fresh = updates_mod.updates(self.oe)
        fresh.LOCAL_UPDATE_DIR = self.update_dir + os.sep
        fresh.load_values()
        self.assertEqual(fresh.struct['update']['settings']['Channel']['value'],
                         channels_mod.TESTING)

    def test_a_corrupt_marker_is_treated_as_never_migrated(self):
        self.old_install(SettingsVersion='nonsense', AutoUpdate='auto')
        self.module.load_values()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'manual')


class TestProviderRouting(UpdatesTestCase):

    def test_release_uses_github(self):
        self.set_channel(channels_mod.RELEASE)
        provider = self.module.get_provider()
        self.assertIsInstance(provider, providers_mod.GitHubReleasesProvider)
        self.assertEqual(provider.repo, channels_mod.GITHUB_REPO)

    def test_prerelease_toggle_reaches_the_provider(self):
        self.set_channel(channels_mod.RELEASE)
        self.assertFalse(self.module.get_provider().include_prereleases)
        self.settings()['ShowPrereleases']['value'] = '1'
        self.assertTrue(self.module.get_provider().include_prereleases)

    def test_testing_uses_the_public_mega_folder(self):
        self.set_channel(channels_mod.TESTING)
        provider = self.module.get_provider()
        self.assertIsInstance(provider, providers_mod.MegaFolderProvider)
        self.assertEqual(provider.link, channels_mod.TESTING_LINK)

    def test_internal_without_an_unlock_has_no_provider(self):
        self.set_channel(channels_mod.INTERNAL)
        self.assertIsNone(self.module.get_provider())

    def test_internal_uses_the_unlocked_link(self):
        self.set_channel(channels_mod.INTERNAL)
        self.module.internal_link = INTERNAL_LINK
        provider = self.module.get_provider()
        self.assertIsInstance(provider, providers_mod.MegaFolderProvider)
        self.assertEqual(provider.link, INTERNAL_LINK)

    def test_mega_errors_surface_as_a_message_not_a_crash(self):
        provider = FakeProvider([], list_error=MegaError(-17))
        self.assertEqual(self.module.list_builds(provider), [])
        self.assertTrue(any('bandwidth' in str(m) for m in kodi_stubs.FakeDialog.messages()))

    def test_provider_errors_are_shown_in_plain_words(self):
        """A GitHub rate limit explains itself; repr() of it does not."""
        provider = FakeProvider(
            [], list_error=providers_mod.ProviderError('API rate limit exceeded for you.'))
        self.assertEqual(self.module.list_builds(provider), [])
        messages = [str(m) for m in kodi_stubs.FakeDialog.messages()]
        self.assertTrue(any('API rate limit exceeded' in m for m in messages))
        self.assertFalse(any('ProviderError(' in m for m in messages))


class TestAStoredLinkThatNoLongerParses(UpdatesTestCase):
    """A settings file can hold anything; the internal link is read from one.

    Building a provider parses the link, and an unparseable one raises out of
    get_provider. Both callers swallow it, so the effect is a Build button that
    does nothing at all and a poll that fails every six hours behind a log
    line. Whatever else happens, the user has to be told which of the two
    things they can do about it.
    """

    BROKEN = (
        'https://mega.nz/folder/truncated',            # no key
        'https://mega.nz/folder/EXAMPLE1#tooshort',    # key is not 16 bytes
        'not a link at all',
        '   ',
        )

    def setUp(self):
        super().setUp()
        self.oe.settings[('updates', 'SettingsVersion')] = str(updates_mod.SETTINGS_VERSION)
        self.oe.settings[('updates', 'Channel')] = channels_mod.INTERNAL

    def load_with(self, link):
        self.oe.settings[('updates', 'InternalLink')] = link
        self.module.load_values()

    def test_no_provider_is_built_from_one(self):
        for link in self.BROKEN:
            with self.subTest(link=link):
                self.load_with(link)
                self.assertIsNone(self.module.get_provider())

    def test_the_picker_says_to_unlock_rather_than_doing_nothing(self):
        self.load_with(self.BROKEN[0])
        self.module.do_manual_update()
        self.assertIn(STRINGS[32050], kodi_stubs.FakeDialog.messages())

    def test_a_poll_does_not_raise(self):
        self.load_with(self.BROKEN[0])
        self.module.check_updates()
        self.assertEqual(self.oe.notifications, [])

    def test_the_broken_link_is_not_kept(self):
        """Held on to, it would look unlocked forever while serving nothing."""
        self.load_with(self.BROKEN[0])
        self.assertEqual(self.module.internal_link, '')

    def test_a_good_link_is_still_kept(self):
        self.load_with(INTERNAL_LINK)
        self.assertEqual(self.module.internal_link, INTERNAL_LINK)

    def test_a_link_that_arrives_after_startup_is_still_not_fatal(self):
        """unlock_internal writes whatever the blob decrypted to."""
        self.load_with(INTERNAL_LINK)
        self.module.internal_link = 'https://mega.nz/folder/truncated'
        self.assertIsNone(self.module.get_provider())


class TestUnlockInternal(UpdatesTestCase):

    def setUp(self):
        super().setUp()
        self.original_blob = channels_mod.INTERNAL_BLOB
        channels_mod.INTERNAL_BLOB = channels_mod.seal(INTERNAL_LINK, 'hunter2')
        self.addCleanup(setattr, channels_mod, 'INTERNAL_BLOB', self.original_blob)

    def test_correct_password_persists_the_link(self):
        kodi_stubs.FakeKeyboard.answers = [('hunter2', True)]
        self.module.unlock_internal()
        self.assertEqual(self.module.internal_link, INTERNAL_LINK)
        self.assertEqual(self.oe.settings[('updates', 'InternalLink')], INTERNAL_LINK)

    def test_password_entry_is_hidden(self):
        kodi_stubs.FakeKeyboard.answers = [('hunter2', True)]
        self.module.unlock_internal()
        self.assertTrue(kodi_stubs.FakeKeyboard.headings[0][1], 'keyboard must be hidden')

    def test_wrong_password_persists_nothing(self):
        kodi_stubs.FakeKeyboard.answers = [('wrong', True)]
        self.module.unlock_internal()
        self.assertEqual(self.module.internal_link, '')
        self.assertNotIn(('updates', 'InternalLink'), self.oe.settings)
        self.assertIn(STRINGS[32034], kodi_stubs.FakeDialog.messages())

    def test_cancelled_entry_does_nothing(self):
        kodi_stubs.FakeKeyboard.answers = [('', False)]
        self.module.unlock_internal()
        self.assertEqual(self.module.internal_link, '')
        self.assertEqual(kodi_stubs.FakeDialog.calls, [])

    def test_unprovisioned_build_says_so_without_prompting(self):
        channels_mod.INTERNAL_BLOB = ''
        self.module.unlock_internal()
        self.assertEqual(kodi_stubs.FakeKeyboard.headings, [])
        self.assertIn(STRINGS[32036], kodi_stubs.FakeDialog.messages())


class TestBuildPicker(UpdatesTestCase):

    def setUp(self):
        super().setUp()
        self.set_channel(channels_mod.TESTING)
        # These exercise the demotion rule, which only has anything to do when
        # auto-update is on. It does not ship on, so say so here rather than
        # leaning on whatever the shipped default happens to be.
        self.settings()['AutoUpdate']['value'] = 'auto'
        self.builds = make_builds(NEWEST, MIDDLE, RUNNING)
        self.provider = FakeProvider(self.builds, payload=b'x' * 64)
        self.module.get_provider = lambda channel=None: self.provider

    def test_lists_every_build_newest_first(self):
        kodi_stubs.FakeDialog.select_result = -1
        self.module.do_manual_update()
        labels = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'select'][0][2]
        self.assertEqual(len(labels), 3)
        self.assertIn('2026-07-24 16:13', labels[0])

    def test_marks_newest_and_running(self):
        kodi_stubs.FakeDialog.select_result = -1
        self.module.do_manual_update()
        labels = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'select'][0][2]
        self.assertIn(STRINGS[32044], labels[0])          # newest
        self.assertIn(STRINGS[32043], labels[2])          # running

    def test_cancelling_the_picker_changes_nothing(self):
        kodi_stubs.FakeDialog.select_result = -1
        self.module.do_manual_update()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'auto')
        self.assertFalse(hasattr(self.module, 'update_in_progress'))

    def test_choosing_an_older_build_disables_auto_update(self):
        """Otherwise the poll thread silently reinstalls the newest build."""
        kodi_stubs.FakeDialog.select_result = 2      # the running/oldest build
        kodi_stubs.FakeDialog.yesno_answers = [True, False]
        self.module.do_manual_update()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'manual')
        self.assertEqual(self.oe.settings[('updates', 'AutoUpdate')], 'manual')

    def test_the_rollback_warning_is_shown_before_confirming(self):
        kodi_stubs.FakeDialog.select_result = 1
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.do_manual_update()
        confirm = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'yesno'][0][2]
        self.assertIn('Automatic updates will be switched off', confirm)

    def test_choosing_the_newest_build_keeps_auto_update(self):
        kodi_stubs.FakeDialog.select_result = 0
        kodi_stubs.FakeDialog.yesno_answers = [True, False]
        self.module.do_manual_update()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'auto')

    def test_declining_the_confirmation_does_not_demote(self):
        kodi_stubs.FakeDialog.select_result = 2
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.do_manual_update()
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'auto')

    def test_manual_mode_shows_the_plain_confirmation(self):
        self.settings()['AutoUpdate']['value'] = 'manual'
        kodi_stubs.FakeDialog.select_result = 2
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.do_manual_update()
        confirm = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'yesno'][0][2]
        self.assertNotIn('switched off', confirm)

    def test_empty_channel_reports_instead_of_offering_nothing(self):
        self.provider.builds = []
        self.module.do_manual_update()
        self.assertIn(STRINGS[32041], kodi_stubs.FakeDialog.messages())

    def test_a_locked_internal_channel_says_to_unlock_it(self):
        """'Not available in this build' is wrong when it is merely locked."""
        self.set_channel(channels_mod.INTERNAL)
        self.module.internal_link = ''
        self.module.do_manual_update()
        messages = kodi_stubs.FakeDialog.messages()
        self.assertIn(STRINGS[32050], messages)
        self.assertNotIn(STRINGS[32036], messages)
        self.assertNotIn(STRINGS[32041], messages)

    def test_an_unprovisioned_internal_channel_still_says_so(self):
        """No blob compiled in is a different problem from a locked channel."""
        self.set_channel(channels_mod.INTERNAL)
        self.module.internal_link = ''
        original = channels_mod.INTERNAL_BLOB
        channels_mod.INTERNAL_BLOB = ''
        self.addCleanup(setattr, channels_mod, 'INTERNAL_BLOB', original)
        self.module.do_manual_update()
        messages = kodi_stubs.FakeDialog.messages()
        self.assertIn(STRINGS[32036], messages)
        self.assertNotIn(STRINGS[32050], messages)

    def test_the_serial_is_shown_on_the_testing_channel(self):
        """The last four digits are what the dev quotes with the patch notes."""
        self.set_channel(channels_mod.TESTING)
        kodi_stubs.FakeDialog.select_result = -1
        self.module.do_manual_update()
        labels = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'select'][0][2]
        self.assertIn('T4c_dev 1307', labels[0])

    def test_the_serial_is_shown_on_the_internal_channel(self):
        self.set_channel(channels_mod.INTERNAL)
        self.module.internal_link = INTERNAL_LINK
        kodi_stubs.FakeDialog.select_result = -1
        self.module.do_manual_update()
        labels = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'select'][0][2]
        self.assertIn('T4c_dev 1307', labels[0])

    def test_the_serial_is_left_off_the_release_channel(self):
        """Release builds are announced by tag and release notes, not a serial."""
        self.set_channel(channels_mod.RELEASE)
        kodi_stubs.FakeDialog.select_result = -1
        self.module.do_manual_update()
        labels = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'select'][0][2]
        self.assertIn('T4c_dev', labels[0])
        self.assertNotIn('T4c_dev 1307', labels[0])

    def test_picker_is_capped(self):
        many = [build_mod.make_build(
            f'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4_2026070400{i:04d}.tar',
            ref='r') for i in range(40)]
        self.provider.builds = many
        kodi_stubs.FakeDialog.select_result = -1
        self.module.do_manual_update()
        labels = [c for c in kodi_stubs.FakeDialog.calls if c[0] == 'select'][0][2]
        self.assertEqual(len(labels), providers_mod.PICKER_LIMIT)


class InstallTestCase(UpdatesTestCase):

    def setUp(self):
        super().setUp()
        self.payload = b'p3i-build-payload' * 4096
        self.entry = make_builds(NEWEST)[0]
        self.entry.size = len(self.payload)
        self.sync_calls = []
        self._real_call = updates_mod.subprocess.call
        updates_mod.subprocess.call = lambda *a, **k: self.sync_calls.append(a)
        self.addCleanup(setattr, updates_mod.subprocess, 'call', self._real_call)

    def staged(self):
        if not os.path.isdir(self.update_dir):
            return []
        return os.listdir(self.update_dir)

    def leftovers(self):
        """Files left directly in TEMP. The download names its own, so this
        asks the question the fixed 'update_file' name used to answer."""
        return sorted(p for p in glob.glob(self.oe.TEMP + '*') if os.path.isfile(p))


class TestInstall(InstallTestCase):

    def test_verified_build_is_staged_for_the_next_boot(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]      # decline the reboot
        self.assertTrue(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.staged(), [NEWEST])
        with open(os.path.join(self.update_dir, NEWEST), 'rb') as handle:
            self.assertEqual(handle.read(), self.payload)

    def test_nothing_is_left_in_temp(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.install(self.entry, provider, silent=True)
        self.assertEqual(self.leftovers(), [])

    def test_sync_runs_before_the_reboot_prompt(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.install(self.entry, provider, silent=False)
        self.assertTrue(self.sync_calls)

    def test_the_visible_path_offers_to_reboot(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.install(self.entry, provider, silent=False)
        self.assertIn(STRINGS[32045], kodi_stubs.FakeDialog.messages())

    def test_accepting_the_prompt_reboots(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [True]
        self.module.install(self.entry, provider, silent=False)
        self.assertTrue(sys.modules['xbmc'].restart_calls)

    def test_checksum_mismatch_discards_the_download(self):
        provider = FakeProvider([self.entry], payload=self.payload, digest='0' * 64)
        self.assertFalse(self.module.install(self.entry, provider, silent=False))
        self.assertEqual(self.staged(), [])
        self.assertEqual(self.leftovers(), [])
        self.assertIn(STRINGS[32038], kodi_stubs.FakeDialog.messages())

    def test_missing_sidecar_discards_the_download(self):
        """Both channels publish one, so absence means something is wrong."""
        provider = FakeProvider([self.entry], payload=self.payload, digest=None)
        self.assertFalse(self.module.install(self.entry, provider, silent=False))
        self.assertEqual(self.staged(), [])
        self.assertIn(STRINGS[32039], kodi_stubs.FakeDialog.messages())

    def test_a_failed_sidecar_fetch_is_treated_as_missing(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        provider.fetch_sha256 = lambda entry: (_ for _ in ()).throw(OSError('boom'))
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.staged(), [])

    def test_insufficient_space_aborts_before_downloading(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        self.module.free_space = lambda path: 1024
        self.assertFalse(self.module.install(self.entry, provider, silent=False))
        self.assertEqual(provider.streams, [])
        self.assertIn(STRINGS[32037], kodi_stubs.FakeDialog.messages())

    def test_cancelling_mid_download_discards_the_partial_file(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeProgressDialog.cancel_after = 1
        self.assertFalse(self.module.install(self.entry, provider, silent=False))
        self.assertEqual(self.staged(), [])
        self.assertEqual(self.leftovers(), [])

    def test_a_mega_error_while_opening_is_reported(self):
        provider = FakeProvider([self.entry], open_error=MegaError(-17))
        self.assertFalse(self.module.install(self.entry, provider, silent=False))
        self.assertTrue(any('bandwidth' in str(m) for m in kodi_stubs.FakeDialog.messages()))

    def test_an_http_error_while_opening_is_reported(self):
        """The GitHub channel fails with ordinary exceptions, not MegaError."""
        provider = FakeProvider([self.entry], open_error=OSError('connection refused'))
        self.assertFalse(self.module.install(self.entry, provider, silent=False))
        self.assertTrue(kodi_stubs.FakeDialog.calls, 'the user must be told')
        self.assertTrue(any('connection refused' in str(m)
                            for m in kodi_stubs.FakeDialog.messages()))

    def test_a_dropped_connection_discards_the_partial_file(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        original_open = provider.open

        def failing_open(entry):
            stream = original_open(entry)
            reads = [0]

            def read(amount):
                reads[0] += 1
                if reads[0] > 2:
                    raise OSError('connection reset')
                return FakeStream.read(stream, amount)

            stream.read = read
            return stream

        provider.open = failing_open
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.leftovers(), [],
                         'a partial download must not be left on /storage')
        self.assertEqual(self.staged(), [])

    def test_the_stream_is_closed_even_on_failure(self):
        provider = FakeProvider([self.entry], payload=self.payload, digest='0' * 64)
        self.module.install(self.entry, provider, silent=True)
        self.assertTrue(provider.streams[0].closed)


class TestWhatIsVerifiedIsWhatIsStaged(InstallTestCase):
    """The digest has to come from the file, not from the bytes that made it.

    The poll thread and the picker can both be downloading at once: auto-update
    is on, a build starts downloading unattended, and the user opens the menu
    and picks one. Hashing the network stream means each side verifies its own
    bytes while the file on disk holds a mixture of the two - and initramfs
    flashes that mixture. Reading the staged candidate back closes it, and
    catches a bad write on the way.
    """

    def corrupting_provider(self):
        """Rewrites the downloaded file the moment the download finishes.

        Stands in for the other install racing us: same window, same effect on
        the file, without needing two threads to interleave on cue.
        """
        provider = FakeProvider([self.entry], payload=self.payload)
        answer = provider.fetch_sha256

        def fetch_sha256(entry):
            for path in self.leftovers():
                with open(path, 'wb') as handle:
                    handle.write(b'bytes from the other download')
            return answer(entry)

        provider.fetch_sha256 = fetch_sha256
        return provider

    def test_a_file_changed_after_the_download_fails_verification(self):
        self.assertFalse(
            self.module.install(self.entry, self.corrupting_provider(), silent=True))

    def test_nothing_is_staged_when_the_file_no_longer_matches(self):
        self.module.install(self.entry, self.corrupting_provider(), silent=True)
        self.assertEqual(self.staged(), [])

    def test_the_mismatch_is_reported_as_one(self):
        self.module.install(self.entry, self.corrupting_provider(), silent=False)
        self.assertIn(STRINGS[32038], kodi_stubs.FakeDialog.messages())

    def test_the_corrupt_candidate_is_not_left_behind(self):
        self.module.install(self.entry, self.corrupting_provider(), silent=True)
        self.assertEqual(self.leftovers(), [])

    def test_an_untouched_download_still_verifies(self):
        """The disk read must agree with the stream on the ordinary path."""
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.assertTrue(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.staged(), [NEWEST])

    def test_two_downloads_do_not_share_one_temp_file(self):
        """A fixed name is what let the two interleave in the first place."""
        seen = []
        provider = FakeProvider([self.entry], payload=self.payload)

        def open_and_look(entry):
            seen.append(self.leftovers())
            return FakeStream(self.payload)

        provider.open = open_and_look
        kodi_stubs.FakeDialog.yesno_answers = [False, False]
        self.module.install(self.entry, provider, silent=True)
        self.module.install(self.entry, provider, silent=True)
        self.assertEqual([len(s) for s in seen], [1, 1], 'one download in flight each time')
        self.assertNotEqual(seen[0], seen[1],
                            'a fixed temp name lets two downloads write over each other')

    def test_a_failed_open_does_not_leave_an_empty_file_behind(self):
        """mkstemp creates the file before the download can fail."""
        provider = FakeProvider([self.entry], open_error=OSError('connection refused'))
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.leftovers(), [])

    def test_an_unexpected_failure_does_not_leave_one_behind_either(self):
        """A name of its own means an orphan is an orphan for good.

        With one fixed name a leaked file was overwritten by the next attempt.
        Now every attempt names its own, so anything not cleaned up accumulates
        on /storage a quarter of a gigabyte at a time.
        """
        provider = FakeProvider([self.entry], payload=self.payload)
        original = updates_mod.shutil.move
        updates_mod.shutil.move = lambda *a: (_ for _ in ()).throw(OSError('read-only'))
        self.addCleanup(setattr, updates_mod.shutil, 'move', original)
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.leftovers(), [])

    def test_repeated_failures_do_not_accumulate(self):
        provider = FakeProvider([self.entry], payload=self.payload, digest='0' * 64)
        for _ in range(3):
            self.module.install(self.entry, provider, silent=True)
        self.assertEqual(self.leftovers(), [])

    def test_two_installs_cannot_both_stage_a_build(self):
        """Clearing the directory and moving into it has to be one step.

        Separate temp files stop the two downloads corrupting each other, but
        both still finish, and if each reads an empty directory before either
        moves, both tars end up staged. initramfs then installs whichever sorts
        first and deletes the other unread - so the build the user picked can
        lose to the one the poll thread happened to fetch.

        Made deterministic by parking the first install inside the directory
        scan, which is where the interleaving has to happen for it to matter.
        """
        import threading as _threading

        os.makedirs(self.update_dir, exist_ok=True)
        other = build_mod.make_build(MIDDLE, ref=MIDDLE, size=len(self.payload))
        in_scan = _threading.Event()
        release = _threading.Event()
        real_scan = self.module.staged_builds

        def scan_once():
            self.module.staged_builds = real_scan       # only the first install
            found = real_scan()                         # empty: nothing staged yet
            in_scan.set()
            release.wait(5)
            return found                                # ... and now stale

        self.module.staged_builds = scan_once
        kodi_stubs.FakeDialog.yesno_answers = [False, False]

        first = _threading.Thread(target=self.module.install, args=(
            self.entry, FakeProvider([self.entry], payload=self.payload), True))
        first.start()
        self.assertTrue(in_scan.wait(5), 'first install never reached the scan')

        second = _threading.Thread(target=self.module.install, args=(
            other, FakeProvider([other], payload=self.payload), True))
        second.start()
        second.join(0.5)          # long enough to stage, if nothing stops it
        release.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())

        self.assertEqual(len(self.staged()), 1,
                         'two builds staged: initramfs installs one and deletes the other')


class TestABuildNameCannotChooseItsOwnDestination(InstallTestCase):
    """entry.name is decrypted from the channel, not chosen by us.

    Mega allows '/' in a filename, so a name is attacker-controlled input the
    moment a channel folder is compromised or a build is uploaded carelessly.
    os.path.join with it writes wherever the name says.
    """

    def build_named(self, name):
        entry = build_mod.make_build(name, ref=name, size=len(self.payload))
        self.assertIsNotNone(entry, 'fixture must survive make_build')
        return entry

    def install_named(self, name):
        entry = self.build_named(name)
        provider = FakeProvider([entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        return self.module.install(entry, provider, silent=True)

    def test_a_relative_escape_lands_in_the_update_directory(self):
        self.assertTrue(self.install_named('../' + NEWEST))
        self.assertEqual(self.staged(), [NEWEST])

    def test_a_relative_escape_writes_nothing_outside_it(self):
        self.install_named('../' + NEWEST)
        self.assertFalse(os.path.exists(os.path.join(self.temp, NEWEST)),
                         'the build was written outside /storage/.update')

    def test_a_deep_escape_is_flattened_too(self):
        self.install_named('../../' + NEWEST)
        self.assertEqual(self.staged(), [NEWEST])

    def test_an_absolute_name_does_not_pick_its_own_path(self):
        absolute = os.path.join(self.temp, 'elsewhere', NEWEST)
        self.assertTrue(self.install_named(absolute))
        self.assertEqual(self.staged(), [NEWEST])
        self.assertFalse(os.path.exists(absolute))

    def test_a_subdirectory_in_the_name_is_dropped(self):
        """initramfs globs *.tar in the directory itself, not below it."""
        self.install_named('archive/' + NEWEST)
        self.assertEqual(self.staged(), [NEWEST])

    def test_an_ordinary_name_is_untouched(self):
        self.assertTrue(self.install_named(NEWEST))
        self.assertEqual(self.staged(), [NEWEST])


class TestStagingReplacesThePreviousBuild(InstallTestCase):
    """Two staged .tar files are worse than one out-of-date box.

    initramfs takes `ls -1 "$UPDATE_DIR"/*.tar | head -n 1` and then wipes the
    directory, so leaving an older tar behind installs *that* one and destroys
    the build the user actually asked for.
    """

    def setUp(self):
        super().setUp()
        os.makedirs(self.update_dir)

    def stage_stale(self, name=MIDDLE, size=16):
        path = os.path.join(self.update_dir, name)
        with open(path, 'wb') as handle:
            handle.write(b'\0' * size)
        return path

    def test_only_the_new_build_is_left_staged(self):
        self.stage_stale()
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.assertTrue(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.staged(), [NEWEST])

    def test_a_failed_install_leaves_the_previous_build_alone(self):
        """Clearing before the new build verifies would strand the box."""
        self.stage_stale()
        provider = FakeProvider([self.entry], payload=self.payload, digest='0' * 64)
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.staged(), [MIDDLE])

    def test_files_that_are_not_builds_are_left_alone(self):
        """initramfs owns this directory; only *.tar is ours to remove."""
        keep = os.path.join(self.update_dir, '.nocompat')
        open(keep, 'w').close()
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.install(self.entry, provider, silent=True)
        self.assertTrue(os.path.exists(keep))

    def test_restaging_the_same_name_still_works(self):
        self.stage_stale(name=NEWEST)
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.assertTrue(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.staged(), [NEWEST])
        with open(os.path.join(self.update_dir, NEWEST), 'rb') as handle:
            self.assertEqual(handle.read(), self.payload)

    def test_a_staged_build_does_not_excuse_a_full_filesystem(self):
        """Its bytes are not free yet: it is deleted after the download, not before.

        Crediting them lets the download start with nowhere to put it, and the
        box hits ENOSPC a few hundred megabytes in instead of being told up
        front that there is no room.
        """
        self.stage_stale(size=len(self.payload))
        self.module.free_space = lambda path: updates_mod.FREE_SPACE_MARGIN + 16
        provider = FakeProvider([self.entry], payload=self.payload)
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(provider.streams, [], 'the download must not be started')
        self.assertEqual(self.staged(), [MIDDLE], 'the staged build must survive')

    def test_space_is_refused_when_there_is_none(self):
        self.module.free_space = lambda path: 1024
        provider = FakeProvider([self.entry], payload=self.payload)
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(provider.streams, [])

    def test_an_unreadable_free_space_figure_does_not_refuse(self):
        """statvfs returning nothing means unknown, which is not the same as full."""
        self.module.free_space = lambda path: 0
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.assertTrue(self.module.install(self.entry, provider, silent=True))

    def test_a_directory_that_looks_like_a_build_is_ignored(self):
        """os.remove cannot clear one, so counting it would strand the updater."""
        os.makedirs(os.path.join(self.update_dir, 'bogus.tar'))
        self.assertEqual(self.module.staged_builds(), [])

    def test_a_directory_that_looks_like_a_build_is_left_alone(self):
        bogus = os.path.join(self.update_dir, 'bogus.tar')
        os.makedirs(bogus)
        provider = FakeProvider([self.entry], payload=self.payload)
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.assertTrue(self.module.install(self.entry, provider, silent=True))
        self.assertTrue(os.path.isdir(bogus))


class TestSilentInstallNeverBlocksTheUi(InstallTestCase):
    """The 6-hourly poll runs on a background thread.

    A modal raised there sits on top of whatever the user is doing until
    somebody presses OK, which is not acceptable for a check they never asked
    for. The old do_autoupdate path only logged.
    """

    def modals(self):
        return [c for c in kodi_stubs.FakeDialog.calls if c[0] in ('ok', 'yesno', 'select')]

    def notified(self, text):
        return any(text in message for _title, message in self.oe.notifications)

    def test_a_checksum_mismatch_notifies_instead_of_prompting(self):
        provider = FakeProvider([self.entry], payload=self.payload, digest='0' * 64)
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.modals(), [])
        self.assertTrue(self.notified(STRINGS[32038]))

    def test_a_missing_sidecar_notifies_instead_of_prompting(self):
        provider = FakeProvider([self.entry], payload=self.payload, digest=None)
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.modals(), [])
        self.assertTrue(self.notified(STRINGS[32039]))

    def test_insufficient_space_notifies_instead_of_prompting(self):
        self.module.free_space = lambda path: 1024
        provider = FakeProvider([self.entry], payload=self.payload)
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.modals(), [])
        self.assertTrue(self.notified(STRINGS[32037]))

    def test_a_failed_open_notifies_instead_of_prompting(self):
        provider = FakeProvider([self.entry], open_error=MegaError(-17))
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.modals(), [])
        self.assertTrue(self.notified('bandwidth'))

    def test_a_dropped_connection_notifies_instead_of_prompting(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        original_open = provider.open

        def failing_open(entry):
            stream = original_open(entry)
            stream.read = lambda amount: (_ for _ in ()).throw(OSError('connection reset'))
            return stream

        provider.open = failing_open
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.modals(), [])
        self.assertTrue(self.notified('connection reset'))

    def test_the_visible_path_still_uses_a_dialog(self):
        """Someone who pressed a button is owed an answer they must dismiss."""
        provider = FakeProvider([self.entry], payload=self.payload, digest='0' * 64)
        self.assertFalse(self.module.install(self.entry, provider, silent=False))
        self.assertIn(STRINGS[32038], kodi_stubs.FakeDialog.messages())

    def test_a_silent_success_does_not_offer_to_reboot(self):
        """Nobody asked for this download, so nobody asked to be interrupted.

        updateThread already reminds them once an hour (32048) for as long as
        the build sits there, which is the right way to raise it.
        """
        provider = FakeProvider([self.entry], payload=self.payload)
        self.assertTrue(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.modals(), [])
        self.assertEqual(sys.modules['xbmc'].restart_calls, [])

    def test_a_silent_success_still_stages_the_build(self):
        provider = FakeProvider([self.entry], payload=self.payload)
        self.module.install(self.entry, provider, silent=True)
        self.assertEqual(self.staged(), [NEWEST])

    def test_a_silent_success_still_announces_the_download(self):
        """The toast is fine; it is the modal that is not."""
        provider = FakeProvider([self.entry], payload=self.payload)
        self.module.install(self.entry, provider, silent=True)
        self.assertTrue(self.notified(STRINGS[32366]))


class TestErrorNotificationsFitOnScreen(InstallTestCase):
    """oe.notify hard-truncates the message at 64 characters, mid-word.

    German runs longer than English for the same sentence, so this has to hold
    at runtime rather than by keeping the English short and hoping.
    """

    # Errors whose wording is fixed, so it can be kept short enough to survive.
    # 32042 is not one: it carries a reason of unbounded length.
    FIXED = (32037, 32038, 32039)

    def test_a_message_that_fits_is_shown_verbatim(self):
        self.module.report(STRINGS[32037], silent=True)
        self.assertEqual(self.oe.notifications[0][1], STRINGS[32037])

    def test_an_over_long_message_is_replaced_rather_than_cut(self):
        self.module.report('word ' * 30, silent=True)
        message = self.oe.notifications[0][1]
        self.assertEqual(message, STRINGS[32049])
        self.assertLessEqual(len(message), updates_mod.NOTIFY_MAX_LEN)

    def test_the_modal_path_keeps_the_full_wording(self):
        long_message = 'word ' * 30
        self.module.report(long_message, silent=False)
        self.assertIn(long_message, kodi_stubs.FakeDialog.messages())
        self.assertEqual(self.oe.notifications, [])

    def test_a_long_failure_reason_arrives_as_a_sentence(self):
        """A name resolution failure carries far more text than fits."""
        provider = FakeProvider([self.entry], open_error=OSError(
            '<urlopen error [Errno -3] Temporary failure in name resolution>'))
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertEqual(self.oe.notifications[0][1], STRINGS[32049])

    def test_a_short_failure_reason_stays_specific(self):
        """Falling back further than necessary would lose a useful message."""
        provider = FakeProvider([self.entry], open_error=MegaError(-17))
        self.assertFalse(self.module.install(self.entry, provider, silent=True))
        self.assertIn('bandwidth', self.oe.notifications[0][1])

    def test_the_fixed_wordings_fit_in_english(self):
        for code in self.FIXED:
            self.assertLessEqual(
                len(STRINGS[code]), updates_mod.NOTIFY_MAX_LEN,
                f'#{code} would be replaced by the generic form on a silent update')

    def test_the_fixed_wordings_fit_in_every_translation(self):
        for path in sorted(glob.glob(os.path.join(ROOT, 'language', '*', 'strings.po'))):
            language = os.path.basename(os.path.dirname(path))
            strings = load_translations(path)
            for code in self.FIXED:
                if code not in strings:
                    continue
                self.assertLessEqual(
                    len(strings[code]), updates_mod.NOTIFY_MAX_LEN,
                    f'{language} #{code} would be replaced by the generic form')


class TestPolling(UpdatesTestCase):

    def setUp(self):
        super().setUp()
        self.set_channel(channels_mod.TESTING)
        # Auto-update does not ship on; the tests below that care about the
        # unattended path turn it on explicitly rather than inheriting it.
        self.settings()['AutoUpdate']['value'] = 'auto'
        self.payload = b'z' * 2048
        self.newest = make_builds(NEWEST)[0]
        self.newest.size = len(self.payload)
        self.provider = FakeProvider([self.newest], payload=self.payload)
        self.module.get_provider = lambda channel=None: self.provider
        self._real_call = updates_mod.subprocess.call
        updates_mod.subprocess.call = lambda *a, **k: None
        self.addCleanup(setattr, updates_mod.subprocess, 'call', self._real_call)

    def test_notifies_when_a_newer_build_exists(self):
        self.settings()['AutoUpdate']['value'] = 'manual'
        self.module.check_updates()
        self.assertIn((STRINGS[32363], STRINGS[32364]), self.oe.notifications)

    def test_silent_when_nothing_is_newer(self):
        self.provider.builds = make_builds(RUNNING)
        self.module.check_updates()
        self.assertEqual(self.oe.notifications, [])

    def test_older_builds_do_not_trigger_an_update(self):
        self.provider.builds = make_builds(
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4a_20260101000000.tar')
        self.module.check_updates()
        self.assertEqual(self.oe.notifications, [])
        self.assertFalse(hasattr(self.module, 'update_in_progress'))

    def test_auto_mode_downloads_and_stages(self):
        kodi_stubs.FakeDialog.yesno_answers = [False]
        self.module.check_updates()
        self.assertEqual(os.listdir(self.update_dir), [NEWEST])

    def test_manual_mode_does_not_download(self):
        self.settings()['AutoUpdate']['value'] = 'manual'
        self.module.check_updates()
        self.assertEqual(self.provider.streams, [])

    def test_force_suppresses_the_automatic_download(self):
        self.module.check_updates(force=True)
        self.assertEqual(self.provider.streams, [])

    def test_notifications_can_be_switched_off(self):
        self.settings()['UpdateNotify']['value'] = '0'
        self.settings()['AutoUpdate']['value'] = 'manual'
        self.module.check_updates()
        self.assertEqual(self.oe.notifications, [])

    def test_a_pending_update_short_circuits_the_poll(self):
        self.module.update_in_progress = True
        self.module.check_updates()
        self.assertEqual(self.provider.streams, [])

    def test_a_failed_install_clears_the_in_progress_flag(self):
        """Otherwise polling stops forever after one bad download."""
        self.provider._digest = '0' * 64
        self.module.check_updates()
        self.assertFalse(hasattr(self.module, 'update_in_progress'))


class TestNotificationsAreNotRepeated(UpdatesTestCase):

    def setUp(self):
        super().setUp()
        self.set_channel(channels_mod.TESTING)
        self.settings()['AutoUpdate']['value'] = 'manual'
        self.provider = FakeProvider(make_builds(NEWEST))
        self.module.get_provider = lambda channel=None: self.provider

    def test_the_same_build_is_announced_only_once(self):
        """Polling every 6h must not mean a popup every 6h forever."""
        for _ in range(4):
            self.module.check_updates()
        self.assertEqual(len(self.oe.notifications), 1)

    def test_a_genuinely_new_build_is_announced(self):
        self.module.check_updates()
        self.provider.builds = make_builds(
            'CoreELEC-Amlogic-ng.arm-21.3-Omega_p3i_T4c_dev_20260801000000.tar')
        self.module.check_updates()
        self.assertEqual(len(self.oe.notifications), 2)

    def test_the_announced_build_is_persisted(self):
        self.module.check_updates()
        self.assertEqual(self.oe.settings[('updates', 'LastNotified')], '20260724161307')

    def test_a_reboot_does_not_restart_the_reminders(self):
        self.module.check_updates()
        fresh = updates_mod.updates(self.oe)
        fresh.LOCAL_UPDATE_DIR = self.update_dir + os.sep
        fresh.load_values()
        self.assertEqual(fresh.last_notified, 20260724161307)

    def test_notifications_off_announces_nothing(self):
        self.settings()['UpdateNotify']['value'] = '0'
        self.module.check_updates()
        self.assertEqual(self.oe.notifications, [])


class TestPollThread(UpdatesTestCase):

    def one_iteration(self):
        """Run exactly one pass of the thread loop; return the wait it chose."""
        self.oe.dictModules['updates'] = self.module
        self.module.check_updates = lambda: None
        thread = updates_mod.updateThread(self.oe)
        chosen = []

        def wait_once(timeout=None):
            thread.stopped = True
            chosen.append(timeout)

        thread.wait_evt.wait = wait_once
        thread.run()
        return chosen[0]

    def test_idle_poll_interval(self):
        self.assertEqual(self.one_iteration(), updates_mod.POLL_INTERVAL)
        self.assertEqual(self.oe.notifications, [])

    def test_staged_update_reminds_the_user_to_reboot(self):
        self.module.update_in_progress = True
        self.assertEqual(self.one_iteration(), updates_mod.POLL_INTERVAL_PENDING)
        self.assertEqual(self.oe.notifications, [(STRINGS[32363], STRINGS[32048])])

    def test_the_reminder_says_downloaded_not_available(self):
        """32364 ('Update available') is wrong once the build is on disk."""
        self.module.update_in_progress = True
        self.one_iteration()
        message = self.oe.notifications[0][1]
        self.assertNotEqual(message, STRINGS[32364])
        self.assertIn('reboot', message.lower())

    def test_the_reminder_honours_the_notification_setting(self):
        self.settings()['UpdateNotify']['value'] = '0'
        self.module.update_in_progress = True
        self.assertEqual(self.one_iteration(), updates_mod.POLL_INTERVAL_PENDING)
        self.assertEqual(self.oe.notifications, [], 'UpdateNotify=0 must be silent')

    def test_nothing_is_announced_during_playback(self):
        kodi_stubs.FakePlayer.playing = True
        self.module.update_in_progress = True
        self.one_iteration()
        self.assertEqual(self.oe.notifications, [])

    def test_the_thread_does_not_hold_up_a_shutdown(self):
        """stop() sets an event the thread may be nowhere near waiting on.

        A poll caught in the Mega retry ladder is inside a socket timeout, not
        inside wait_evt, so it answers a stop request up to a couple of minutes
        late. Non-daemon, that is a couple of minutes Kodi cannot exit in.
        """
        self.assertTrue(updates_mod.updateThread(self.oe).daemon)


class TestSettingsStruct(UpdatesTestCase):

    def test_obsolete_settings_are_gone(self):
        removed = ['SubmitStats', 'Update2NextStable', 'ShowCustomChannels',
                   'CustomChannel1', 'CustomChannel2', 'CustomChannel3']
        for name in removed:
            self.assertNotIn(name, self.settings())

    def test_rpi_eeprom_category_is_gone(self):
        self.assertNotIn('rpieeprom', self.module.struct)

    def test_every_string_id_in_the_struct_resolves(self):
        for category in self.module.struct.values():
            for setting in category['settings'].values():
                for key in ('name', 'InfoText'):
                    value = setting.get(key)
                    if isinstance(value, int):
                        self.oe._(value)      # raises if undefined

    def test_prereleases_is_gated_to_the_release_channel(self):
        parent = self.settings()['ShowPrereleases']['parent']
        self.assertEqual(parent['entry'], 'Channel')
        self.assertEqual(parent['value'], [channels_mod.RELEASE])

    def test_unlock_is_gated_to_the_internal_channel(self):
        parent = self.settings()['InternalUnlock']['parent']
        self.assertEqual(parent['entry'], 'Channel')
        self.assertEqual(parent['value'], [channels_mod.INTERNAL])

    def test_auto_update_ships_switched_off(self):
        """A box does not install a build until somebody asks it to."""
        self.assertEqual(self.settings()['AutoUpdate']['value'], 'manual')

    def test_notifications_ship_switched_on(self):
        self.assertEqual(self.settings()['UpdateNotify']['value'], '1')

    def test_channel_offers_exactly_the_three_channels(self):
        self.assertEqual(self.settings()['Channel']['values'], list(channels_mod.CHANNELS))

    def test_the_channel_list_is_not_reordered_around_the_selection(self):
        """oeWindows floats the current value to the top of a multivalue list.

        For a device list that is helpful; for three fixed channels it means
        the order changes every time one is picked, so the setting opts out.
        """
        self.assertTrue(self.settings()['Channel'].get('keep_order'))

    def test_the_channels_are_offered_in_their_declared_order(self):
        self.assertEqual(list(channels_mod.CHANNELS),
                         [channels_mod.RELEASE, channels_mod.TESTING, channels_mod.INTERNAL])

    def test_every_action_is_implemented(self):
        for category in self.module.struct.values():
            for name, setting in category['settings'].items():
                self.assertTrue(hasattr(self.module, setting['action']),
                                f'{name} -> {setting["action"]} is missing')


if __name__ == '__main__':
    unittest.main()
