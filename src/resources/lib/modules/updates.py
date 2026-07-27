# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2009-2013 Stephan Raue (stephan@openelec.tv)
# Copyright (C) 2013 Lutz Fiebach (lufie@openelec.tv)
# Copyright (C) 2018 Team LibreELEC
# Copyright (C) 2018-present Team CoreELEC (https://coreelec.org)

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading

import xbmc
import xbmcgui

from updater import build as build_mod
from updater import channels as channels_mod
from updater import providers as providers_mod
from updater.mega_client import MegaError, parse_folder_link

CHUNK_SIZE = 32768

# Headroom over the build itself for the filesystem and the staged copy.
FREE_SPACE_MARGIN = 32 * 1024 * 1024

POLL_INTERVAL = 21600      # 6h
POLL_INTERVAL_PENDING = 3600

# oe.notify slices the message at this length before handing it to Kodi, and it
# cuts wherever it lands rather than at a word boundary.
NOTIFY_MAX_LEN = 64

# Bumped whenever a settings file written by an older addon needs correcting on
# read. Tracked in the settings file itself so each correction runs exactly
# once and cannot overrule a choice the user makes afterwards.
SETTINGS_VERSION = 1


class updates:

    ENABLED = False
    LOCAL_UPDATE_DIR = None

    menu = {'2': {
        'name': 32005,
        'menuLoader': 'load_menu',
        'listTyp': 'list',
        'InfoText': 707,
        }}

    def __init__(self, oeMain):
        try:
            oeMain.dbg_log('updates::__init__', 'enter_function', oeMain.LOGDEBUG)
            self.oe = oeMain
            self.struct = {
                'update': {
                    'order': 1,
                    'name': 32013,
                    'settings': {
                        'AutoUpdate': {
                            'name': 32014,
                            # Off by default: a box should not install a build
                            # until somebody has asked it to. UpdateNotify
                            # below is what makes that a workable default.
                            'value': 'manual',
                            'action': 'set_auto_update',
                            'type': 'multivalue',
                            'values': ['auto', 'manual'],
                            'InfoText': 714,
                            'order': 1,
                            },
                        'UpdateNotify': {
                            'name': 32365,
                            'value': '1',
                            'action': 'set_value',
                            'type': 'bool',
                            'InfoText': 715,
                            'order': 2,
                            },
                        'Channel': {
                            'name': 32015,
                            'value': channels_mod.DEFAULT_CHANNEL,
                            'action': 'set_channel',
                            'type': 'multivalue',
                            'values': list(channels_mod.CHANNELS),
                            # Three fixed channels in a deliberate order, so
                            # floating the current one to the top of the list
                            # just makes the menu move under the user.
                            'keep_order': True,
                            'InfoText': 760,
                            'order': 3,
                            },
                        'ShowPrereleases': {
                            'name': 32031,
                            'value': '0',
                            'action': 'set_prereleases',
                            'type': 'bool',
                            'parent': {
                                'entry': 'Channel',
                                'value': [channels_mod.RELEASE],
                                },
                            'InfoText': 32046,
                            'order': 4,
                            },
                        'InternalUnlock': {
                            'name': 32032,
                            'value': '',
                            'action': 'unlock_internal',
                            'type': 'button',
                            'parent': {
                                'entry': 'Channel',
                                'value': [channels_mod.INTERNAL],
                                },
                            'InfoText': 32047,
                            'order': 5,
                            },
                        'Build': {
                            'name': 32020,
                            'value': '',
                            'action': 'do_manual_update',
                            'type': 'button',
                            'InfoText': 770,
                            'order': 6,
                            },
                        },
                    },
                }

            self.internal_link = ''
            self.last_notified = 0
            # Serialises the last step of install(). Two downloads can be in
            # flight at once - the poll thread stages a build unattended while
            # the user picks one from the menu - and each ends by clearing the
            # update directory and moving its own build in. Interleave those and
            # both survive, which is the one thing initramfs cannot cope with.
            self.stage_lock = threading.Lock()
            self.oe.dbg_log('updates::__init__', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::__init__', 'ERROR: (' + repr(e) + ')')

    # ------------------------------------------------------------------ service

    def start_service(self):
        try:
            self.oe.dbg_log('updates::start_service', 'enter_function', self.oe.LOGDEBUG)
            self.is_service = True
            self.load_values()
            self.set_auto_update()
            del self.is_service
            self.oe.dbg_log('updates::start_service', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::start_service', 'ERROR: (' + repr(e) + ')')

    def stop_service(self):
        try:
            self.oe.dbg_log('updates::stop_service', 'enter_function', self.oe.LOGDEBUG)
            if hasattr(self, 'update_thread'):
                self.update_thread.stop()
            self.oe.dbg_log('updates::stop_service', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::stop_service', 'ERROR: (' + repr(e) + ')')

    def do_init(self):
        pass

    def exit(self):
        pass

    # ------------------------------------------------------------------ settings

    def load_values(self):
        try:
            self.oe.dbg_log('updates::load_values', 'enter_function', self.oe.LOGDEBUG)

            settings = self.struct['update']['settings']

            value = self.oe.read_setting('updates', 'AutoUpdate')
            if value is not None:
                settings['AutoUpdate']['value'] = value

            value = self.oe.read_setting('updates', 'UpdateNotify')
            if value is not None:
                settings['UpdateNotify']['value'] = value

            value = self.oe.read_setting('updates', 'ShowPrereleases')
            if value is not None:
                settings['ShowPrereleases']['value'] = value

            # Existing installs hold a CoreELEC train name here ('21-ng'), which
            # matches no channel we serve. Coerced on read rather than migrated,
            # so a hand-edited or truncated value is corrected too.
            settings['Channel']['value'] = channels_mod.resolve_channel(
                self.oe.read_setting('updates', 'Channel'))

            # Dropped rather than kept if it no longer parses. A link we cannot
            # build a provider from is indistinguishable from no link at all,
            # and holding on to one leaves the channel looking unlocked while
            # serving nothing; empty at least routes the user to the password
            # button, which is what rewrites this value.
            stored_link = self.oe.read_setting('updates', 'InternalLink') or ''
            self.internal_link = stored_link if self.usable_link(stored_link) else ''
            if stored_link and not self.internal_link:
                self.oe.dbg_log('updates::load_values',
                                'stored internal link does not parse; ignoring',
                                self.oe.LOGERROR)

            # Which build the user has already been told about, so the same one
            # is not announced on every poll. Persisted, so a reboot does not
            # start the reminders over.
            value = self.oe.read_setting('updates', 'LastNotified')
            self.last_notified = int(value) if value and value.isdigit() else 0

            self.migrate_settings()

            # A staged build outlives the addon, so it has to be recognised on
            # every start. Missing it means the next poll downloads a quarter
            # of a gigabyte again and stages a second tar beside the first.
            if self.staged_builds() or os.path.isfile('%s/SYSTEM' % self.LOCAL_UPDATE_DIR):
                self.update_in_progress = True

            self.oe.dbg_log('updates::load_values', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::load_values', 'ERROR: (' + repr(e) + ')')

    def migrate_settings(self):
        """Correct a settings file written by an older addon. Runs once.

        Called after the persisted values have been read, so it overrides them,
        and gated on a version stored alongside them, so a user who changes one
        of these back is not overruled on the next start.
        """
        try:
            stored = self.oe.read_setting('updates', 'SettingsVersion')
            version = int(stored) if stored and stored.isdigit() else 0
            if version >= SETTINGS_VERSION:
                return

            settings = self.struct['update']['settings']

            if version < 1:
                # The CoreELEC update service this replaced is gone, and the
                # builds on offer now come from somewhere else entirely. An
                # 'auto' carried over from that service was consent to install
                # Team CoreELEC's releases unattended, not p3i's, so it is
                # withdrawn - and the announcement is turned on in its place so
                # nobody simply stops hearing about updates.
                settings['AutoUpdate']['value'] = 'manual'
                settings['UpdateNotify']['value'] = '1'
                self.oe.write_setting('updates', 'AutoUpdate', 'manual')
                self.oe.write_setting('updates', 'UpdateNotify', '1')

                # Back to Release for the same reason. A legacy CoreELEC train
                # name already lands there via resolve_channel; this is for a
                # box left on Testing or Internal by an earlier build of this
                # addon, which should not keep pulling dev builds by default.
                # Any unlocked internal link is left in place, so getting back
                # there costs one menu change rather than the password again.
                settings['Channel']['value'] = channels_mod.DEFAULT_CHANNEL
                self.oe.write_setting('updates', 'Channel', channels_mod.DEFAULT_CHANNEL)

            self.oe.write_setting('updates', 'SettingsVersion', str(SETTINGS_VERSION))
            self.oe.dbg_log('updates::migrate_settings',
                            'migrated %d -> %d' % (version, SETTINGS_VERSION),
                            self.oe.LOGINFO)
        except Exception as e:
            self.oe.dbg_log('updates::migrate_settings', 'ERROR: (' + repr(e) + ')')

    def load_menu(self, focusItem):
        try:
            self.oe.dbg_log('updates::load_menu', 'enter_function', self.oe.LOGDEBUG)
            self.oe.winOeMain.build_menu(self.struct)
            self.oe.dbg_log('updates::load_menu', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::load_menu', 'ERROR: (' + repr(e) + ')')

    def set_value(self, listItem):
        try:
            self.struct[listItem.getProperty('category')]['settings'][listItem.getProperty('entry')]['value'] = \
                listItem.getProperty('value')
            self.oe.write_setting('updates', listItem.getProperty('entry'), str(listItem.getProperty('value')))
        except Exception as e:
            self.oe.dbg_log('updates::set_value', 'ERROR: (' + repr(e) + ')')

    def set_auto_update(self, listItem=None):
        try:
            self.oe.dbg_log('updates::set_auto_update', 'enter_function', self.oe.LOGDEBUG)
            if listItem is not None:
                self.set_value(listItem)
            if not hasattr(self, 'update_disabled'):
                if not hasattr(self, 'update_thread'):
                    self.update_thread = updateThread(self.oe)
                    self.update_thread.start()
                else:
                    self.update_thread.wait_evt.set()
            self.oe.dbg_log('updates::set_auto_update',
                            str(self.struct['update']['settings']['AutoUpdate']['value']), self.oe.LOGINFO)
            self.oe.dbg_log('updates::set_auto_update', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::set_auto_update', 'ERROR: (' + repr(e) + ')')

    def set_channel(self, listItem=None):
        try:
            if listItem is not None:
                self.set_value(listItem)
            self.struct['update']['settings']['Channel']['value'] = channels_mod.resolve_channel(
                self.struct['update']['settings']['Channel']['value'])
        except Exception as e:
            self.oe.dbg_log('updates::set_channel', 'ERROR: (' + repr(e) + ')')

    def set_prereleases(self, listItem=None):
        try:
            if listItem is not None:
                self.set_value(listItem)
        except Exception as e:
            self.oe.dbg_log('updates::set_prereleases', 'ERROR: (' + repr(e) + ')')

    def _set_auto_update_value(self, value):
        """Force AutoUpdate, persisting it so the UI reflects the change."""
        self.struct['update']['settings']['AutoUpdate']['value'] = value
        self.oe.write_setting('updates', 'AutoUpdate', value)

    # ------------------------------------------------------------------ channels

    def current_channel(self):
        return channels_mod.resolve_channel(self.struct['update']['settings']['Channel']['value'])

    def should_notify(self):
        """Whether the user wants update notifications at all."""
        return self.struct['update']['settings']['UpdateNotify']['value'] == '1'

    def unlock_internal(self, listItem=None):
        """Prompt for the internal channel password and store the opened link.

        The password is typed hidden and never persisted; only the link it
        decrypts is written to the settings file, so background update checks
        keep working across a reboot.
        """
        try:
            self.oe.dbg_log('updates::unlock_internal', 'enter_function', self.oe.LOGDEBUG)
            if not channels_mod.is_provisioned(channels_mod.INTERNAL):
                xbmcgui.Dialog().ok(self.oe._(32032), self.oe._(32036))
                return

            keyboard = xbmc.Keyboard('', self.oe._(32033), True)
            keyboard.doModal()
            if not keyboard.isConfirmed():
                return

            try:
                link = channels_mod.unseal(channels_mod.INTERNAL_BLOB, keyboard.getText())
            except channels_mod.WrongPassword:
                xbmcgui.Dialog().ok(self.oe._(32032), self.oe._(32034))
                return
            except channels_mod.NotProvisioned:
                xbmcgui.Dialog().ok(self.oe._(32032), self.oe._(32036))
                return

            self.internal_link = link
            self.oe.write_setting('updates', 'InternalLink', link)
            xbmcgui.Dialog().ok(self.oe._(32032), self.oe._(32035))
            self.oe.dbg_log('updates::unlock_internal', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::unlock_internal', 'ERROR: (' + repr(e) + ')')

    @staticmethod
    def usable_link(link):
        """Whether a Mega folder link is one we could actually open."""
        try:
            parse_folder_link(link)
            return True
        except ValueError:
            return False

    def get_provider(self, channel=None):
        """Provider for a channel, or None if it cannot be served yet.

        None rather than an exception for a link that will not parse. Building
        the provider is where the link is read, and both callers treat a raised
        exception as an unexplained failure: the picker button does nothing at
        all and the poll fails every six hours behind a log line.
        """
        channel = channel or self.current_channel()
        builder = self.oe.BUILDER_NAME or 'p3i'

        if channel == channels_mod.RELEASE:
            return providers_mod.GitHubReleasesProvider(
                channels_mod.GITHUB_REPO,
                include_prereleases=(self.struct['update']['settings']['ShowPrereleases']['value'] == '1'),
                builder=builder,
                )

        if channel == channels_mod.TESTING:
            link = channels_mod.TESTING_LINK
        elif channel == channels_mod.INTERNAL:
            link = self.internal_link
        else:
            return None

        if not link:
            return None
        try:
            return providers_mod.MegaFolderProvider(link, builder=builder)
        except ValueError as e:
            self.oe.dbg_log('updates::get_provider',
                            '%s link is unusable: %s' % (channel, str(e)), self.oe.LOGERROR)
            return None

    def list_builds(self, provider, notify_errors=True):
        """Builds in a channel, newest first. [] on any source failure."""
        try:
            self.oe.set_busy(1)
            return provider.list_builds()
        except (MegaError, providers_mod.ProviderError) as e:
            # Both carry wording written for a user; repr() would bury it.
            self.oe.dbg_log('updates::list_builds', 'source: %s' % str(e), self.oe.LOGERROR)
            if notify_errors:
                xbmcgui.Dialog().ok(self.oe._(32363), self.oe._(32042) % str(e))
            return []
        except Exception as e:
            self.oe.dbg_log('updates::list_builds', 'ERROR: (' + repr(e) + ')')
            if notify_errors:
                xbmcgui.Dialog().ok(self.oe._(32363), self.oe._(32042) % repr(e))
            return []
        finally:
            self.oe.set_busy(0)

    # ------------------------------------------------------------------ picker

    def do_manual_update(self, listItem=None):
        """Show every build in the channel and install the chosen one."""
        try:
            self.oe.dbg_log('updates::do_manual_update', 'enter_function', self.oe.LOGDEBUG)
            channel = self.current_channel()

            if channel == channels_mod.INTERNAL and not self.internal_link:
                # Locked and not provisioned are different problems with
                # different answers: the first the user can fix from the menu
                # right above this button, the second they cannot fix at all.
                locked = channels_mod.is_provisioned(channels_mod.INTERNAL)
                xbmcgui.Dialog().ok(self.oe._(32363), self.oe._(32050 if locked else 32036))
                return

            provider = self.get_provider(channel)
            if provider is None:
                xbmcgui.Dialog().ok(self.oe._(32363), self.oe._(32036))
                return

            builds = self.list_builds(provider)
            if not builds:
                xbmcgui.Dialog().ok(self.oe._(32363), self.oe._(32041))
                return

            builds = builds[:providers_mod.PICKER_LIMIT]
            running = build_mod.find_running(builds, self.oe.VERSION)
            # Dev builds are referred to by their serial in Discord, where the
            # patch notes are posted, so on those channels it is the field a
            # tester matches on. Release builds have a tag and release notes.
            show_serial = channel in (channels_mod.TESTING, channels_mod.INTERNAL)
            labels = []
            for index, entry in enumerate(builds):
                label = entry.label(serial=show_serial)
                if index == 0:
                    label = '%s   (%s)' % (label, self.oe._(32044))
                if running is not None and entry.timestamp == running.timestamp:
                    label = '%s   <- %s' % (label, self.oe._(32043))
                labels.append(label)

            selection = xbmcgui.Dialog().select('%s - %s' % (channel, self.oe._(32020)), labels)
            if selection < 0:
                return
            chosen = builds[selection]

            message = '%s: %s\n%s: %s' % (
                self.oe._(32188), self.oe.VERSION,
                self.oe._(32187), chosen.name,
                )
            # Rolling back with auto-update still on means the poll thread
            # reinstalls the newest build within the hour, silently.
            demotes_auto = (selection != 0
                            and self.struct['update']['settings']['AutoUpdate']['value'] == 'auto')
            if demotes_auto:
                message = '%s\n\n%s' % (message, self.oe._(32040) % channel)
            else:
                message = '%s\n\n%s' % (message, self.oe._(32180))

            if not xbmcgui.Dialog().yesno(self.oe._(32363), message):
                return

            if demotes_auto:
                self._set_auto_update_value('manual')

            self.update_in_progress = True
            if not self.install(chosen, provider, silent=False):
                if hasattr(self, 'update_in_progress'):
                    del self.update_in_progress
            self.oe.dbg_log('updates::do_manual_update', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::do_manual_update', 'ERROR: (' + repr(e) + ')')

    # ------------------------------------------------------------------ polling

    def check_updates(self, force=False):
        """Poll the active channel. Called by the update thread and on demand."""
        try:
            self.oe.dbg_log('updates::check_updates', 'enter_function', self.oe.LOGDEBUG)
            if hasattr(self, 'update_in_progress'):
                self.oe.dbg_log('updates::check_updates', 'update in progress (exit)', self.oe.LOGDEBUG)
                return

            provider = self.get_provider()
            if provider is None:
                return

            builds = self.list_builds(provider, notify_errors=False)
            candidate = build_mod.newest(builds)
            if candidate is None:
                return
            if not build_mod.is_newer_than_running(candidate, self.oe.VERSION):
                return

            # Announce a given build once, not on every poll. Keyed on the build
            # rather than a flag, so a genuinely new build still gets announced
            # while the same one never nags twice.
            if self.should_notify() and candidate.timestamp != self.last_notified:
                self.oe.notify(self.oe._(32363), self.oe._(32364))
                self.last_notified = candidate.timestamp
                self.oe.write_setting('updates', 'LastNotified', str(candidate.timestamp))

            if self.struct['update']['settings']['AutoUpdate']['value'] == 'auto' and not force:
                self.update_in_progress = True
                if not self.install(candidate, provider, silent=True):
                    if hasattr(self, 'update_in_progress'):
                        del self.update_in_progress
            self.oe.dbg_log('updates::check_updates', 'exit_function', self.oe.LOGDEBUG)
        except Exception as e:
            self.oe.dbg_log('updates::check_updates', 'ERROR: (' + repr(e) + ')')

    # ------------------------------------------------------------------ install

    def free_space(self, path):
        try:
            stat = os.statvfs(path)
            return stat.f_bavail * stat.f_frsize
        except Exception:
            return 0

    def staged_builds(self):
        """Builds already sitting in LOCAL_UPDATE_DIR, waiting for a reboot.

        initramfs takes the first *.tar it finds there and then wipes the whole
        directory, so a second staged build does not queue behind the first: it
        replaces it, oldest wins, and the newer one is deleted unread. Anything
        that is not a .tar belongs to initramfs and is left alone.
        """
        try:
            names = os.listdir(self.LOCAL_UPDATE_DIR)
        except OSError:
            return []
        paths = [os.path.join(self.LOCAL_UPDATE_DIR, name)
                 for name in names if name.endswith('.tar')]
        # Regular files only. A directory carrying the suffix is not a build,
        # and os.remove cannot clear it, so treating it as one would leave the
        # updater permanently convinced an update is pending.
        return [path for path in paths if os.path.isfile(path)]

    def file_digest(self, path):
        """sha256 of a file on disk, read back in the same chunks as the download."""
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def report(self, message, silent):
        """Tell the user an update failed, without taking over their screen.

        The poll runs every six hours on a background thread, behind whatever
        is on screen. A modal raised from there blocks playback until somebody
        walks over and presses OK, so the unattended path gets a notification
        instead. Not gated on UpdateNotify: that setting is about announcing
        builds, and a failure is not an announcement.
        """
        if not silent:
            xbmcgui.Dialog().ok(self.oe._(32363), message)
            return

        # Anything too long for a notification is replaced outright rather than
        # shown as a fragment ending mid-word. Wordings we control are short
        # enough to survive; the ones carrying a failure reason are not, since
        # the reason has no length limit. Full text goes to the log regardless.
        if len(message) > NOTIFY_MAX_LEN:
            message = self.oe._(32049)
        self.oe.notify(self.oe._(32363), message)

    def install(self, entry, provider, silent=False):
        """Download, verify against the sidecar, and stage for the next boot.

        Nothing is flashed here: a verified .tar is moved into LOCAL_UPDATE_DIR
        and initramfs applies it on reboot, exactly as before.
        """
        temp_path = None
        try:
            self.oe.dbg_log('updates::install', 'enter_function', self.oe.LOGDEBUG)

            if not os.path.exists(self.LOCAL_UPDATE_DIR):
                os.makedirs(self.LOCAL_UPDATE_DIR)

            if entry.size:
                available = self.free_space(self.oe.TEMP)
                # Zero means statvfs could not tell us, which is not the same
                # as no space; only a real figure is allowed to refuse.
                #
                # An already-staged build is not counted as free even though it
                # is about to be deleted: that deletion happens once the new
                # build has been verified, so its bytes stay occupied for the
                # whole download. Crediting them here would let the download
                # start with nowhere to put it.
                if available and available < entry.size + FREE_SPACE_MARGIN:
                    self.oe.dbg_log('updates::install',
                                    'insufficient space: %d < %d' % (available, entry.size),
                                    self.oe.LOGERROR)
                    self.report(self.oe._(32037), silent)
                    return False

            # A name of its own, not a fixed one. The poll thread and the picker
            # can be downloading at the same time - auto-update stages a build
            # unattended while the user opens the menu and picks another - and a
            # shared path means the two interleave into a single file.
            handle, temp_path = tempfile.mkstemp(prefix='update_', dir=self.oe.TEMP)
            os.close(handle)

            progress = self.oe.ProgressDialog()
            # ProgressDialog.update() splits source on '/', so it needs one.
            progress.setSource('%s/%s' % (self.current_channel(), entry.name))

            cancelled = False
            try:
                stream = provider.open(entry)
            except Exception as e:
                # Includes MegaError, which carries a human-readable reason, and
                # ordinary HTTP failures on the GitHub channel. Either way the
                # user pressed a button and is owed an answer.
                reason = str(e) if isinstance(e, MegaError) else repr(e)
                self.oe.dbg_log('updates::install', 'open failed: %s' % reason, self.oe.LOGERROR)
                self.report(self.oe._(32042) % reason, silent)
                return False

            try:
                progress.setSize(stream.size or entry.size)
                if not silent:
                    progress.open()

                with open(temp_path, 'wb') as target:
                    while not (progress.iscanceled() or self.oe.xbmcm.abortRequested()):
                        chunk = stream.read(CHUNK_SIZE)
                        progress.sample(chunk)
                        if not silent:
                            progress.update(chunk)
                        if not chunk:
                            break
                        target.write(chunk)
                    else:
                        cancelled = True
            except Exception as e:
                # A dropped connection must not leave a part-downloaded build
                # sitting on /storage until the next attempt overwrites it.
                reason = str(e) if isinstance(e, MegaError) else repr(e)
                self.oe.dbg_log('updates::install', 'download failed: %s' % reason,
                                self.oe.LOGERROR)
                self.report(self.oe._(32042) % reason, silent)
                return False
            finally:
                progress.close()
                stream.close()

            if cancelled or progress.iscanceled() or self.oe.xbmcm.abortRequested():
                return False

            expected = None
            try:
                expected = provider.fetch_sha256(entry)
            except Exception as e:
                self.oe.dbg_log('updates::install', 'sha256 fetch failed: ' + repr(e), self.oe.LOGERROR)

            if not expected:
                # Both channels publish a sidecar for every build, so a missing
                # one means something is wrong upstream, not that it is optional.
                self.oe.dbg_log('updates::install', 'no checksum published', self.oe.LOGERROR)
                self.report(self.oe._(32039), silent)
                return False

            # Read back from disk rather than hashing the network stream as it
            # arrives. The stream only proves what was sent; this proves what is
            # about to be staged, which is what initramfs will flash. It also
            # covers a truncated or scribbled-on write, and anything else that
            # reached the file after the download did.
            actual = self.file_digest(temp_path)
            if actual != expected:
                self.oe.dbg_log('updates::install',
                                'checksum mismatch: got %s want %s' % (actual, expected),
                                self.oe.LOGERROR)
                self.report(self.oe._(32038), silent)
                return False

            # One step, under the lock: a scan that another install invalidates
            # before the move lands leaves two builds staged, and initramfs then
            # installs whichever sorts first and deletes the other unread.
            with self.stage_lock:
                # Cleared only now that the new build is verified. Until this
                # point the one already staged is the better of the two to be
                # holding on to, so a failed download leaves the box with a
                # working update rather than none.
                for path in self.staged_builds():
                    self.remove_quietly(path)

                # basename, because entry.name is decrypted out of the channel
                # listing rather than chosen here, and Mega permits '/' in a
                # filename. os.path.join with 'archive/x.tar' or '/etc/x.tar'
                # would take the name at its word and write outside the update
                # directory.
                shutil.move(temp_path,
                            os.path.join(self.LOCAL_UPDATE_DIR,
                                         os.path.basename(entry.name)))
                subprocess.call('sync', shell=True, stdin=None, stdout=None, stderr=None)

            if self.should_notify():
                self.oe.notify(self.oe._(32363), self.oe._(32366))

            # Offered only to someone who asked for this build. The poll thread
            # downloads unattended, and taking the box down from there
            # interrupts whatever is playing; updateThread reminds them once an
            # hour instead (32048) for as long as the build sits staged.
            if not silent and xbmcgui.Dialog().yesno(self.oe._(32363), self.oe._(32045)):
                xbmc.restart()
            self.oe.dbg_log('updates::install', 'exit_function', self.oe.LOGDEBUG)
            return True
        except Exception as e:
            self.oe.dbg_log('updates::install', 'ERROR: (' + repr(e) + ')')
            return False
        finally:
            # Unconditional, because every attempt now names its own file: one
            # missed on an error path is not overwritten by the next attempt,
            # it sits on /storage a quarter of a gigabyte at a time. A no-op on
            # success, where the move has already taken the file away.
            if temp_path:
                self.remove_quietly(temp_path)

    def remove_quietly(self, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            self.oe.dbg_log('updates::remove_quietly', 'ERROR: (' + repr(e) + ')')


class updateThread(threading.Thread):

    def __init__(self, oeMain):
        try:
            oeMain.dbg_log('updates::updateThread::__init__', 'enter_function', oeMain.LOGDEBUG)
            self.oe = oeMain
            self.stopped = False
            self.wait_evt = threading.Event()
            threading.Thread.__init__(self)
            # stop() sets an event this thread may be nowhere near waiting on: a
            # poll caught in the Mega retry ladder is inside a socket timeout,
            # and answers minutes late. Daemon, so that is Kodi's problem to
            # abandon rather than Kodi's shutdown to sit through.
            self.daemon = True
            self.oe.dbg_log('updates::updateThread', 'Started', self.oe.LOGINFO)
        except Exception as e:
            self.oe.dbg_log('updates::updateThread::__init__', 'ERROR: (' + repr(e) + ')')

    def stop(self):
        try:
            self.stopped = True
            self.wait_evt.set()
        except Exception as e:
            self.oe.dbg_log('updates::updateThread::stop()', 'ERROR: (' + repr(e) + ')')

    def run(self):
        try:
            self.oe.dbg_log('updates::updateThread::run', 'enter_function', self.oe.LOGDEBUG)
            while self.stopped is False:
                module = self.oe.dictModules['updates']
                if not xbmc.Player().isPlaying():
                    module.check_updates()
                if not hasattr(module, 'update_in_progress'):
                    self.wait_evt.wait(POLL_INTERVAL)
                else:
                    # A build is already downloaded and staged; this reminds the
                    # user to reboot, so it must say that rather than "update
                    # available", and it honours UpdateNotify like every other
                    # notification.
                    if not xbmc.Player().isPlaying() and module.should_notify():
                        self.oe.notify(self.oe._(32363), self.oe._(32048))
                    self.wait_evt.wait(POLL_INTERVAL_PENDING)
                self.wait_evt.clear()
            self.oe.dbg_log('updates::updateThread', 'Stopped', self.oe.LOGINFO)
        except Exception as e:
            self.oe.dbg_log('updates::updateThread::run', 'ERROR: (' + repr(e) + ')')
