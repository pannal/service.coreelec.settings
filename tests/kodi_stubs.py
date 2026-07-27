# SPDX-License-Identifier: GPL-2.0-or-later
"""Minimal xbmc / xbmcgui stand-ins so the updates module imports off-box.

Only the surface the module actually touches is implemented. Dialog answers are
scripted through class attributes and every call is recorded, so a test can
assert on what the user was shown rather than only on the outcome.
"""

import sys
import types


class FakeDialog:
    # Scripted answers, consumed in order.
    yesno_answers = []
    select_result = -1
    # Every dialog interaction, as (kind, heading, payload).
    calls = []

    @classmethod
    def reset(cls):
        cls.yesno_answers = []
        cls.select_result = -1
        cls.calls = []

    @classmethod
    def messages(cls):
        return [c[2] for c in cls.calls]

    def ok(self, heading, message):
        FakeDialog.calls.append(('ok', heading, message))
        return True

    def yesno(self, heading, message):
        FakeDialog.calls.append(('yesno', heading, message))
        if FakeDialog.yesno_answers:
            return FakeDialog.yesno_answers.pop(0)
        return False

    def select(self, heading, items):
        FakeDialog.calls.append(('select', heading, list(items)))
        return FakeDialog.select_result

    def notification(self, *args, **kwargs):
        FakeDialog.calls.append(('notification', args[0] if args else '', args))


class FakeKeyboard:
    # Scripted (text, confirmed) pairs.
    answers = []
    headings = []

    def __init__(self, default='', heading='', hidden=False):
        self.heading = heading
        self.hidden = hidden
        FakeKeyboard.headings.append((heading, hidden))
        self._text, self._confirmed = (
            FakeKeyboard.answers.pop(0) if FakeKeyboard.answers else ('', False))

    def doModal(self):
        pass

    def isConfirmed(self):
        return self._confirmed

    def getText(self):
        return self._text


class FakePlayer:
    playing = False

    def isPlaying(self):
        return FakePlayer.playing


class FakeProgressDialog:
    """Same surface as oe.ProgressDialog, including its '/' assumption."""

    cancel_after = None      # cancel once this many chunks have been sampled

    def __init__(self, *args, **kwargs):
        self.source = None
        self.total_size = 0
        self.samples = 0
        self.opened = False

    def setSource(self, source):
        # oe.ProgressDialog.update() does source.rsplit('/', 1)[1].
        assert '/' in source, 'progress source must contain a separator'
        self.source = source

    def setSize(self, total_size):
        self.total_size = total_size

    def open(self, *args, **kwargs):
        self.opened = True

    def sample(self, chunk):
        self.samples += 1

    def update(self, chunk):
        pass

    def close(self):
        pass

    def getPercent(self):
        return 0

    def iscanceled(self):
        limit = FakeProgressDialog.cancel_after
        return limit is not None and self.samples >= limit


class FakeMonitor:
    aborted = False

    def abortRequested(self):
        return FakeMonitor.aborted


def install():
    """Put the stubs in sys.modules. Safe to call more than once."""
    if 'xbmc' in sys.modules and getattr(sys.modules['xbmc'], '_is_stub', False):
        return

    xbmc = types.ModuleType('xbmc')
    xbmc._is_stub = True
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
    xbmc.Keyboard = FakeKeyboard
    xbmc.Player = FakePlayer
    xbmc.restart_calls = []
    xbmc.restart = lambda: xbmc.restart_calls.append(True)
    xbmc.log = lambda *a, **k: None

    xbmcgui = types.ModuleType('xbmcgui')
    xbmcgui._is_stub = True
    xbmcgui.Dialog = FakeDialog
    xbmcgui.NOTIFICATION_WARNING = 'warning'
    xbmcgui.NOTIFICATION_INFO = 'info'

    sys.modules['xbmc'] = xbmc
    sys.modules['xbmcgui'] = xbmcgui


def reset():
    FakeDialog.reset()
    FakeKeyboard.answers = []
    FakeKeyboard.headings = []
    FakePlayer.playing = False
    FakeProgressDialog.cancel_after = None
    FakeMonitor.aborted = False
    if 'xbmc' in sys.modules:
        sys.modules['xbmc'].restart_calls = []
