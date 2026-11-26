# SPDX-License-Identifier: GPL-2.0-or-later
"""Check UAS capability and the saved opt-in through the Hardware settings loader."""

import glob
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock

import kodi_stubs

kodi_stubs.install()
ROOT = Path(__file__).resolve().parents[1]
with mock.patch.dict(sys.modules, {'oeWindows': mock.Mock(), 'wifi_profile': mock.Mock()}):
    Hardware = runpy.run_path(str(ROOT / 'src/resources/lib/modules/hardware.py'))['hardware']


class UasVisibilityTests(unittest.TestCase):
    def test_visibility_and_saved_choice_on_reload(self):
        oe = mock.Mock()
        oe.read_setting.return_value = None
        oe.get_config_ini.return_value = '0'
        hardware = Hardware(oe)
        setting = hardware.struct['hdd']['settings']['uas_enable']
        policy = '/sys/bus/usb/drivers/uas/p3i_policy'
        marker = '/storage/.config/uas.enabled'
        module_pattern = '/usr/lib/modules/*/uas/uas.ko'

        with mock.patch.object(hardware, 'fill_values_by_xml'), \
             mock.patch.object(hardware, 'load_wifi_throughput'), \
             mock.patch.object(hardware, 'check_compatibility', return_value=False), \
             mock.patch('os.listdir', return_value=[]), \
             mock.patch('os.path.exists') as exists, mock.patch.object(glob, 'glob') as modules:
            for builtin, legacy in ((False, False), (True, False), (False, False), (False, True)):
                for opted_in in (False, True):
                    with self.subTest(builtin=builtin, legacy=legacy, opted_in=opted_in):
                        paths = {policy: builtin, marker: opted_in}
                        exists.side_effect = paths.get
                        modules.side_effect = {module_pattern: ['uas.ko'] if legacy else []}.__getitem__
                        hardware.load_values()
                        self.assertEqual('hidden' not in setting, builtin or legacy)
                        self.assertEqual(setting['value'], '1' if opted_in else '0')
                        oe.dbg_log.assert_called_with('hardware::load_values', 'exit_function', 0)
        oe.write_setting.assert_not_called()
