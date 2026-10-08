# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise real backup archives off-device, with Kodi dialogs stubbed."""
import ast
import importlib.util
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import kodi_stubs
kodi_stubs.install()

spec = importlib.util.spec_from_file_location('ce_backup_system', ROOT / 'src/resources/lib/modules/system.py')
backup = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, {'oeWindows': types.ModuleType('oeWindows')}):
    spec.loader.exec_module(backup)


class FakeOE:
    LOGDEBUG = 0
    DISTRIBUTION = 'CoreELEC'
    PIN = types.SimpleNamespace(isEnabled=lambda: False)

    def __init__(self):
        self.settings = {}
        self.errors = []

    def dbg_log(self, where, message, *args):
        if 'ERROR' in message:
            self.errors.append((where, message))

    def read_setting(self, module, key):
        return self.settings.get(key)

    def write_setting(self, module, key, value):
        self.settings[key] = value

    def set_busy(self, value):
        pass

    def _(self, code):
        return str(code)

    def timestamp(self):
        return 'backup'


class Progress:
    def create(self, *args):
        pass

    def close(self):
        pass

    def iscanceled(self):
        return False

    def update(self, *args):
        pass


def read_po(path):
    """Parse multiline PO fields, retaining duplicates for reference checks."""
    result, entry, field = {}, {}, None
    for line in path.read_text().splitlines() + ['']:
        if not line.strip():
            if 'msgctxt' in entry:
                result.setdefault(entry['msgctxt'], []).append(entry)
            entry, field = {}, None
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            field, value = line.split(' ', 1)
            entry[field] = ast.literal_eval(value)
        elif line.startswith('"') and field:
            entry[field] += ast.literal_eval(line)
    return result


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Path(self.tmp.name)
        self.oe = FakeOE()
        self.system = backup.system(self.oe)
        self.system.BACKUP_DIRS = [str(self.storage / x) for x in ('.kodi', '.config', '.cache')]
        self.system.BACKUP_LOG_DIR = str(self.storage / '.cache/log')
        self.system.XBMC_THUMBNAILS = str(self.storage / '.kodi/userdata*/Thumbnails')
        self.system.BACKUP_DESTINATION = str(self.storage / 'backup') + '/'
        Path(self.system.BACKUP_DESTINATION).mkdir()
        self.files = {
            '.cache/log/journal/huge.journal': b'L' * 8192,
            '.cache/log/system.log': b'S' * 4096,
            '.cache/logbook/state': b'keep similarly named directory',
            '.cache/journald.conf.d/00_settings.conf': b'logging settings',
            '.config/nested/log/important': b'unrelated log directory',
            '.kodi/temp/kodi.log': b'Kodi diagnostics',
            '.kodi/userdata/Thumbnails/nested/image.jpg': b'thumbnail',
        }
        for name, content in self.files.items():
            path = self.storage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def load_settings(self):
        with mock.patch.object(self.system, 'get_keyboard_layouts', return_value=(None, None, {})):
            self.system.load_values()
        self.assertEqual(self.oe.errors, [])

    def archive(self, thumbnails=False):
        dialog = types.SimpleNamespace(yesno=lambda *a, **kw: thumbnails,
                                      browse=lambda *a: self.system.BACKUP_DESTINATION)
        with mock.patch.object(backup.xbmcgui, 'Dialog', return_value=dialog), \
             mock.patch.object(backup.xbmcgui, 'DialogProgress', Progress, create=True):
            self.system.do_backup()
        self.assertEqual(self.oe.errors, [])
        with tarfile.open(self.system.BACKUP_DESTINATION + 'backup.tar') as archive:
            names = {str(Path('/' + member.name).relative_to(self.storage))
                     for member in archive.getmembers() if member.isfile()}
        expected_size = 1 + sum(len(self.files[name]) for name in names)
        self.assertEqual(self.system.total_backup_size, expected_size)
        self.assertEqual(self.system.done_backup_size, expected_size)
        return names

    def test_persistent_and_option_matrix(self):
        for persistent in ('0', '1'):
            for option in ('0', '1'):
                with self.subTest(persistent=persistent, option=option):
                    self.oe.settings.update(journal_persistent=persistent, backup_exclude_logs=option)
                    self.load_settings()
                    names = self.archive()
                    expected = set(self.files) - {'.kodi/userdata/Thumbnails/nested/image.jpg'}
                    if persistent == option == '1':
                        expected -= {'.cache/log/journal/huge.journal', '.cache/log/system.log'}
                    self.assertEqual(names, expected)

    def test_default_excludes_persistent_logs_and_keeps_thumbnails_on_request(self):
        self.oe.settings['journal_persistent'] = '1'
        self.load_settings()
        self.assertEqual(self.system.struct['backup']['settings']['backup_exclude_logs']['value'], '1')
        self.assertEqual(self.archive(thumbnails=True),
                         set(self.files) - {'.cache/log/journal/huge.journal', '.cache/log/system.log'})

    def test_option_persists_through_setting_action_and_reload(self):
        values = {'category': 'backup', 'entry': 'backup_exclude_logs', 'value': '0'}
        self.system.set_value(types.SimpleNamespace(getProperty=values.get))
        self.oe.settings['journal_persistent'] = '1'
        self.system.struct['backup']['settings']['backup_exclude_logs']['value'] = '1'
        self.load_settings()
        self.assertIn('.cache/log/journal/huge.journal', self.archive())

    def test_excluded_root_and_empty_log_folder(self):
        self.oe.settings['journal_persistent'] = '1'
        self.load_settings()
        self.system.BACKUP_DIRS.append(self.system.BACKUP_LOG_DIR + '/')
        self.assertNotIn('.cache/log/system.log', self.archive())
        for name in ('.cache/log/system.log', '.cache/log/journal/huge.journal'):
            (self.storage / name).unlink()
        names = self.archive()
        self.assertIn('.cache/journald.conf.d/00_settings.conf', names)

    def test_missing_log_directory(self):
        import shutil
        shutil.rmtree(self.system.BACKUP_LOG_DIR)
        self.oe.settings['journal_persistent'] = '1'
        self.load_settings()
        self.assertIn('.kodi/temp/kodi.log', self.archive())

    def test_translations_and_setting_references(self):
        en = read_po(ROOT / 'language/resource.language.en_gb/strings.po')
        de = read_po(ROOT / 'language/resource.language.de_de/strings.po')
        setting = self.system.struct['backup']['settings']['backup_exclude_logs']
        for key in ('name', 'InfoText'):
            context = '#' + str(setting[key])
            self.assertEqual(len(en[context]), 1)
            self.assertEqual(len(de[context]), 1)
            self.assertEqual(en[context][0]['msgid'], de[context][0]['msgid'])
            self.assertTrue(de[context][0]['msgstr'])

    def test_default_log_path_matches_cache_root(self):
        spec = importlib.util.spec_from_file_location('ce_backup_defaults', ROOT / 'src/defaults.py')
        defaults = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(defaults)
        self.assertEqual(defaults.system['BACKUP_LOG_DIR'], defaults.CONFIG_CACHE + '/log')
        self.assertIn(defaults.CONFIG_CACHE, defaults.system['BACKUP_DIRS'])


if __name__ == '__main__':
    unittest.main()
