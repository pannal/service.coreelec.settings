# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise driver capability gating and reboot-pending Wi-Fi preferences."""

import ast
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import kodi_stubs
kodi_stubs.install()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile = load_module('ce_wifi_profile', ROOT / 'src/resources/lib/wifi_profile.py')
with mock.patch.dict(sys.modules, {'wifi_profile': profile,
                                  'oeWindows': types.ModuleType('oeWindows')}):
    hardware = load_module('ce_wifi_hardware', ROOT / 'src/resources/lib/modules/hardware.py')


class FakeOE:
    def __init__(self):
        self.errors = []

    def dbg_log(self, where, message, *args):
        if 'ERROR' in message:
            self.errors.append((where, message))

    def set_busy(self, value):
        pass

    def _(self, code):
        return str(code)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdio = self.root / 'sdio'
        self.sdio.mkdir()
        self.config = self.root / 'modprobe.d/99-p3i-wifi-throughput.conf'
        self.config.parent.mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(profile, 'SDIO_DEVICES', self.sdio).start()
        mock.patch.object(profile, 'CONFIG_FILE', self.config).start()
        self.dialog = mock.Mock()
        mock.patch.object(hardware.xbmcgui, 'Dialog', return_value=self.dialog, create=True).start()
        self.reboot = mock.patch.object(hardware.xbmc, 'executebuiltin', create=True).start()
        self.command = mock.patch.object(hardware.subprocess, 'call').start()
        self.oe = FakeOE()
        self.hardware = hardware.hardware(self.oe)
        self.setting = self.hardware.struct['performance']['settings']['wifi_throughput']

    def device(self, value='0', vendor='0x02d0', device='0xaae8'):
        root = self.sdio / 'mmc1:0001:2'
        root.mkdir()
        (root / 'vendor').write_text(vendor)
        (root / 'device').write_text(device)
        # Match the driver/module capability path without assuming a module name.
        parameters = self.root / 'arbitrary-module/parameters'
        parameters.mkdir(parents=True)
        (root / 'driver').mkdir()
        (root / 'driver/module').symlink_to(parameters.parent, target_is_directory=True)
        if value is not None:
            (parameters / 'p3i_tput_active').write_text(value)
        return parameters

    def toggle(self, value):
        item = mock.Mock()
        item.getProperty.side_effect = lambda name: value if name == 'value' else ''
        self.hardware.set_wifi_throughput(item)

    def tearDown(self):
        self.reboot.assert_not_called()
        self.command.assert_not_called()

    def test_missing_or_old_driver_is_hidden(self):
        self.hardware.load_wifi_throughput()
        self.assertEqual(self.setting['hidden'], 'true')
        self.device(value=None)
        self.hardware.load_wifi_throughput()
        self.assertEqual(self.setting['hidden'], 'true')
        self.toggle('1')
        self.assertFalse(self.config.exists())

    def test_other_radio_or_unsupported_revision_is_hidden(self):
        parameters = self.device(device='0xaae7')
        self.assertIsNone(profile.active_value())
        (self.sdio / 'mmc1:0001:2/device').write_text('0xaae8')
        (parameters / 'p3i_tput_active').write_text('-1')
        self.hardware.load_wifi_throughput()
        self.assertEqual(self.setting['hidden'], 'true')

    def test_supported_default_is_off_and_loading_is_read_only(self):
        self.device()
        self.hardware.load_wifi_throughput()
        self.assertNotIn('hidden', self.setting)
        self.assertEqual(self.setting['value'], '0')
        self.assertFalse(self.config.exists())
        self.assertEqual(self.oe.errors, [])

    def test_save_reload_and_disable_preserve_other_configuration(self):
        parameters = self.device()
        unrelated = self.config.parent / 'dhd_sdio.conf'
        unrelated.write_text('options dhd_sdio firmware_path=/custom/ nvram_path=/custom/\n')
        board_config = self.root / 'config_bcm43752a2_ag.txt'
        board_config.write_text('tput_patch=1\ncountry=DE\n')
        self.toggle('1')
        self.assertEqual(self.config.read_text(), profile.OPTION + '1\n')
        self.assertEqual(self.setting['value'], '1')
        self.hardware = hardware.hardware(self.oe)
        self.hardware.load_wifi_throughput()
        self.assertEqual(self.hardware.struct['performance']['settings']['wifi_throughput']['value'], '1')
        self.toggle('0')
        self.assertEqual(self.config.read_text(), profile.OPTION + '0\n')
        self.assertEqual((parameters / 'p3i_tput_active').read_text(), '0')
        self.assertIn('firmware_path=/custom/', unrelated.read_text())
        self.assertEqual(board_config.read_text(), 'tput_patch=1\ncountry=DE\n')
        self.assertEqual(self.oe.errors, [])

    def test_failed_atomic_save_preserves_previous_choice(self):
        self.device()
        profile.save_value('0')
        self.hardware.load_wifi_throughput()
        with mock.patch.object(profile.os, 'replace', side_effect=OSError('write failed')):
            self.toggle('1')
        self.assertEqual(self.setting['value'], '0')
        self.assertEqual(self.config.read_text(), profile.OPTION + '0\n')
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])
        self.assertTrue(self.oe.errors)
        self.dialog.ok.assert_called_with('33512', '33535')

    def test_invalid_value_cannot_write_options(self):
        self.device()
        self.toggle('1\noptions other setting=1')
        self.assertFalse(self.config.exists())
        self.assertEqual(self.setting['value'], '0')

    def test_manual_config_effective_value_is_shown_until_ui_override(self):
        self.device(value='1')
        self.hardware.load_wifi_throughput()
        self.assertEqual(self.setting['value'], '1')
        self.toggle('0')
        self.assertEqual(profile.saved_value('1'), '0')

    def test_bilingual_labels_and_references(self):
        def parse(path):
            entries, entry, field = {}, {}, None
            for line in path.read_text().splitlines() + ['']:
                if not line.strip():
                    if 'msgctxt' in entry:
                        entries.setdefault(entry['msgctxt'], []).append(entry)
                    entry, field = {}, None
                elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
                    field, value = line.split(' ', 1)
                    entry[field] = ast.literal_eval(value)
                elif line.startswith('"') and field:
                    entry[field] += ast.literal_eval(line)
            return entries
        english = parse(ROOT / 'language/resource.language.en_gb/strings.po')
        german = parse(ROOT / 'language/resource.language.de_de/strings.po')
        for identifier in (32537, 914, 33534, 33535):
            key = '#' + str(identifier)
            self.assertEqual(len(english[key]), 1)
            self.assertEqual(len(german[key]), 1)
            self.assertEqual(english[key][0]['msgid'], german[key][0]['msgid'])
            self.assertTrue(german[key][0]['msgstr'])
        self.assertEqual(self.setting['name'], 32537)
        self.assertEqual(self.setting['InfoText'], 914)
        self.assertEqual(self.setting['action'], 'set_wifi_throughput')


if __name__ == '__main__':
    unittest.main()
