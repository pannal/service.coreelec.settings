# SPDX-License-Identifier: GPL-2.0-or-later

import ast
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kodi_stubs
kodi_stubs.install()
sys.modules['xbmcgui'].WindowXMLDialog = object

formats = {**dict.fromkeys((33675, 33677, 33678, 33689, 33692, 33711, 33712, 33724), ' %s'),
           33651: ' %s %s', 33721: ' %s %s %s', 33722: ' %s %s',
           33726: ' %s %s', 33727: ' %s %s'}
formats.update(dict.fromkeys((33733, 33734, 33737, 33740, 33741, 33743, 33744,
                             33745, 33746, 33747), ' %s'))
formats.update({33735: ' %s %s', 33736: ' %s %s', 33755: ' %s %s'})
oe = types.SimpleNamespace(_=lambda number: str(number) + formats.get(number, ''), __cwd__='addon-path')
log = types.SimpleNamespace(log=mock.Mock(), ERROR=3,
                            log_function=lambda: lambda function: function)
tools = types.SimpleNamespace(notification=mock.Mock())
spec = importlib.util.spec_from_file_location('ce_ethernet_client',
    Path(__file__).resolve().parents[1] / 'src/resources/lib/ethernet_calibration.py')
client = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, {'oe': oe, 'log': log, 'ui_tools': tools}):
    spec.loader.exec_module(client)
NumericEntry = client.NumericEntry


class EthernetDialogTests(unittest.TestCase):
    def test_status_has_separate_rows_and_uses_unmodified_single_line_layout(self):
        skin = ET.parse(Path(__file__).resolve().parents[1] /
                        'skins/Default/1080i/service-CoreELEC-Settings-mainWindow.xml')
        for layout_name in ('itemlayout', 'focusedlayout'):
            groups = skin.findall('.//' + layout_name + '/control')
            rows = [group for group in groups if group.findtext('visible') ==
                    'String.IsEqual(ListItem.Property(entry), Calibration)']
            self.assertEqual(rows, [])
        tree = ast.parse((Path(__file__).resolve().parents[1] /
                          'src/resources/lib/modules/connman.py').read_text())
        module = next(node for node in tree.body if isinstance(node, ast.ClassDef) and
                      node.name == 'connman')
        struct = next(node for node in module.body if isinstance(node, ast.Assign) and
                      node.targets[0].id == 'struct')
        ethernet = next(value for key, value in zip(struct.value.keys, struct.value.values)
                        if isinstance(key, ast.Attribute) and key.attr == 'PATH_TECH_ETHERNET')
        rows = ast.literal_eval(ethernet)['settings']
        self.assertTrue({'CalibrationProfile', 'CalibrationTest', 'CalibrationManual'}.isdisjoint(rows))
        self.assertEqual(rows['CalibrationTiming']['name'], 33732)
        for entry in ('CalibrationHealth', 'CalibrationTiming'):
            self.assertEqual(rows[entry]['action'], '')
            self.assertNotIn(rows[entry]['type'], ('button', 'bool'))
            self.assertLess(rows[entry]['order'], rows['Calibration']['order'])
        for name in ('itemlayout', 'focusedlayout'):
            value = next(node for node in skin.findall('.//' + name + '//control')
                         if node.findtext('info') == 'ListItem.Property(value)')
            self.assertIn('!String.IsEqual(ListItem.Property(Typ), button)',
                          value.findtext('visible'))

    def setUp(self):
        self.dialog = mock.Mock()
        self.status = {'supported': True, 'busy': False,
                       'health': {'state': 'unknown'},
                       'profile': {'state': 'applied', 'kind': 'lan_gigabit', 'configured_index': 30},
                       'timing': {'factory': False, 'index': 30, 'state': 'applied'},
                       'public_catalogue': {'regions': ['Europe'], 'source_date': '2026-11-22'},
                       'public_servers': [
                           {'id': 'scaleway-france', 'label': 'Scaleway - France',
                            'hostname': 'ping.online.net', 'port': 5201,
                            'port_first': 5200, 'port_last': 5209,
                            'unsupported_reason': '', 'status': 'OK 10/2026'},
                           {'id': 'iperf3.moji.fr', 'label': 'Moji - France',
                            'hostname': 'iperf3.moji.fr', 'port': 5201,
                            'port_first': 5200, 'port_last': 5240,
                            'unsupported_reason': '', 'status': 'OK 10/2026'}]}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(client.xbmcgui, 'Dialog', return_value=self.dialog).start()
        mock.patch.object(client.xbmcgui, 'INPUT_IPADDRESS', 1, create=True).start()
        mock.patch.object(client.xbmcgui, 'INPUT_NUMERIC', 2, create=True).start()
        self.request = mock.patch.object(client, 'request', return_value=self.status).start()
        def answer(action, **kwargs):
            if action == 'catalogue':
                offset = kwargs['offset']
                entries = self.status['public_servers']
                end = min(offset + 8, len(entries))
                return {'servers': entries[offset:end], 'next_offset': end if end < len(entries) else None}
            return self.request.return_value
        self.request.side_effect = answer
        self.progress_patcher = mock.patch.object(client, 'progress')
        self.progress = self.progress_patcher.start()
        self.numeric = mock.patch.object(client, 'NumericEntry').start()
        self.numeric.return_value.result = None

    def answers(self, values):
        self.numeric.side_effect = [types.SimpleNamespace(result=value, doModal=mock.Mock())
                                    for value in values]

    def test_saved_profile_is_not_a_green_health_result(self):
        self.assertNotIn('[COLOR green]', client.row_values(self.status)['CalibrationHealth'])
        self.status['health']['state'] = 'healthy'
        self.status['profile']['state'] = 'not_saved'
        self.assertIn('[COLOR green]', client.row_values(self.status)['CalibrationHealth'])

    def test_experimental_https_is_explicit_selected_and_needs_consent_without_discovery(self):
        self.dialog.select.side_effect = [0, 2]
        self.dialog.yesno.return_value = True
        client.open_dialog()
        self.request.assert_any_call('start', public=True, consent=True,
                                     backend='cloudflare_https_experimental')
        self.assertFalse(any(call.args[0] == 'catalogue' for call in self.request.call_args_list))
        self.numeric.assert_not_called()
        self.progress.assert_called_once()
        self.assertIn('33700', self.dialog.select.call_args.args[1])

    def test_https_decline_and_picker_cancel_make_no_traffic_request(self):
        for answers in ([0, 2], [0, -1]):
            self.request.reset_mock()
            self.dialog.select.side_effect = answers
            self.dialog.yesno.return_value = False
            client.open_dialog()
            self.assertFalse(any(call.args[0] == 'start' for call in self.request.call_args_list))

    def test_https_profile_label_does_not_certify_health_or_show_lan_speeds(self):
        self.status['profile'].update(kind='public_error_checked',
                                      traffic_backend='cloudflare_https_experimental')
        values = client.row_values(self.status)
        self.assertIn('33757', client.timing_values(self.status)[1])
        self.assertNotIn('[COLOR green]', values['CalibrationHealth'])
    def test_manual_entry_requires_consent_and_has_no_universal_default(self):
        self.status.pop('timing')
        self.answers(['30'])
        self.dialog.yesno.return_value = True
        client.open_manual_dialog()
        self.numeric.assert_called_once_with('service-CoreELEC-Ethernet-Numeric.xml',
            'addon-path', 'Default', heading=33688, kind=0, default='', minimum=0, maximum=31)
        self.request.assert_called_with('manual', index=30, consent=True)
        self.progress.assert_called_once()
        self.dialog.select.assert_not_called()

    def test_manual_cancel_and_declined_warning_make_no_change(self):
        for answer in (None, '30'):
            with self.subTest(answer=answer):
                self.request.reset_mock()
                self.answers([answer])
                self.dialog.yesno.return_value = False
                client.open_manual_dialog()
                self.request.assert_called_once_with('status')
        self.progress.assert_not_called()

    def test_manual_profile_never_displays_old_measured_speed(self):
        self.status['profile'].update(kind='manual_unverified', configured_index=30)
        self.status['last_verification'] = {'send_bps':940_000_000,'receive_bps':940_000_000}
        rows = client.row_values(self.status)
        self.assertIn('33753', client.timing_values(self.status)[1])
        self.assertNotIn('Mbps', client.timing_values(self.status)[1])
        self.assertNotIn('[COLOR green]', rows['CalibrationHealth'])

    def test_manual_numeric_range_includes_zero_but_not_32(self):
        for text, accepted in (('0', True), ('31', True), ('32', False)):
            entry = NumericEntry(heading=33688,kind=0,default=text,minimum=0,maximum=31)
            entry.getControl = mock.Mock()
            entry.close = mock.Mock()
            entry.confirm()
            self.assertEqual(entry.result, text if accepted else None)
            if not accepted:
                entry.onClick(24)
                self.assertIsNone(entry.result)

    def test_manual_unsupported_busy_and_invalid_index_do_not_mutate(self):
        for change in ({'supported':False},{'busy':True}):
            self.status.update(change)
            client.open_manual_dialog()
            self.status.update(supported=True,busy=False)
        self.numeric.assert_not_called()
        self.progress.reset_mock()
        self.answers(['32'])
        client.open_manual_dialog()
        self.request.assert_called_with('status')
        self.dialog.yesno.assert_not_called()
        self.progress.assert_not_called()

    def test_manual_default_is_current_index_including_zero(self):
        self.status['profile']['index']=0
        self.status['timing']['index']=0
        client.open_manual_dialog()
        self.assertEqual(self.numeric.call_args.kwargs['default'],'0')
        self.request.assert_called_once_with('status')

    def test_manual_success_is_explicitly_unverified(self):
        self.progress_patcher.stop()
        self.request.return_value={'phase':'manually_set','index':30,'busy':False}
        monitor=mock.Mock()
        monitor.abortRequested.return_value=False
        with mock.patch.object(client.xbmcgui,'DialogProgress',create=True), \
                mock.patch.object(client.xbmc,'Monitor',return_value=monitor,create=True):
            client.progress()
        self.dialog.ok.assert_called_with('33620','33692 30')

    def test_unsupported_adapter_does_not_offer_a_scan(self):
        self.status['supported'] = False
        client.open_dialog()
        self.dialog.select.assert_not_called()
        self.request.assert_called_once_with('status')

    def test_reduced_margin_is_separate_from_measured_health(self):
        self.status['health']['state'] = 'healthy'
        self.status['profile']['margin'] = 'reduced'
        rows = client.row_values(self.status)
        self.assertIn('[COLOR green]', rows['CalibrationHealth'])
        self.assertIn('33650', client.timing_values(self.status)[1])
        self.assertNotIn('33650', rows['CalibrationHealth'])

    def test_cancelled_disruption_warning_does_not_start_traffic(self):
        self.dialog.select.return_value = 0
        self.dialog.yesno.return_value = False
        client.open_dialog()
        self.request.assert_called_once_with('status')
        self.dialog.input.assert_not_called()

    def test_public_opt_in_is_forwarded_only_after_explicit_confirmation(self):
        self.dialog.select.side_effect = [0, 1, 1]
        self.dialog.yesno.return_value = False
        client.open_dialog()
        self.request.assert_called_once_with('status')
        self.dialog.select.side_effect = [0, 1, 1]
        self.dialog.yesno.return_value = True
        self.answers(['1.1.1.1', '5202'])
        client.open_dialog()
        self.request.assert_called_with('start', address='1.1.1.1', port=5202,
                                        public=True, consent=True)
        self.progress.assert_called_once()

    def test_back_cancels_each_numeric_prompt(self):
        for answers in ([None], ['192.168.2.79', None]):
            with self.subTest(answers=answers):
                self.request.reset_mock()
                self.dialog.select.side_effect = [0, 0]
                self.dialog.yesno.return_value = True
                self.answers(answers)
                client.open_dialog()
                self.request.assert_called_once_with('status')
        self.progress.assert_not_called()

    def test_ip_and_port_use_numeric_keypads_with_fresh_defaults(self):
        self.dialog.select.side_effect = [0, 0]
        self.dialog.yesno.return_value = True
        self.answers(['192.168.2.79', '5201'])
        with mock.patch.object(client.xbmc, 'Keyboard') as keyboard:
            client.open_dialog()
        keyboard.assert_not_called()
        self.assertEqual(self.numeric.call_args_list, [
            mock.call('service-CoreELEC-Ethernet-Numeric.xml', 'addon-path', 'Default',
                      heading=33644, kind=3, default='0.0.0.0'),
            mock.call('service-CoreELEC-Ethernet-Numeric.xml', 'addon-path', 'Default',
                      heading=33645, kind=0, default='5201')])
        self.request.assert_called_with('start', address='192.168.2.79', port=5201,
                                        public=False, consent=False)

    def test_factory_reset_waits_for_async_completion(self):
        self.dialog.select.return_value = 2
        self.dialog.yesno.return_value = True
        client.open_dialog()
        self.request.assert_called_with('clear')
        self.progress.assert_called_once()
        tools.notification.assert_not_called()

    def test_last_verified_speeds_do_not_certify_changed_live_link(self):
        self.status.update(health={'state': 'link_down'},
                           last_verification={'send_bps': 939_000_000, 'receive_bps': 941_000_000})
        rows = client.row_values(self.status)
        self.assertNotIn('green', rows['CalibrationHealth'])
        with self.english():
            self.assertIn('Upload: 939 Mbps. Download: 941 Mbps.', client.timing_values(self.status)[1])
        self.status['health']['state'] = 'insufficient_traffic'
        with self.english():
            self.assertIn('Upload: 939 Mbps. Download: 941 Mbps.', client.timing_values(self.status)[1])

    def test_refresh_updates_existing_rows_without_rebuilding_or_moving_selection(self):
        window = mock.Mock()
        health, profile = mock.Mock(), mock.Mock()
        window.calibration_rows = {'CalibrationHealth': health, 'CalibrationTiming': profile}
        window.calibration_positions = {}
        self.status['health']['state'] = 'corruption'
        client.refresh_rows(window)
        health.setProperty.assert_called_with('value', '[COLOR red]33622[/COLOR]')
        health.getProperty.assert_not_called()
        window.getControl.assert_not_called()

    def test_refresh_discards_response_after_page_rebuild(self):
        old = mock.Mock()
        window = types.SimpleNamespace(calibration_rows={'CalibrationHealth': old}, calibration_positions={})
        def rebuild(**kwargs):
            window.calibration_rows = {}
            return {'CalibrationHealth': 'new status'}, 'new help'
        with mock.patch.object(client, 'menu_status', side_effect=rebuild):
            client.refresh_rows(window)
        old.setProperty.assert_not_called()

    def test_empty_page_does_not_read_native_items_or_fetch_status(self):
        window = mock.Mock(calibration_rows={})
        client.refresh_rows(window)
        self.request.assert_not_called()
        window.getControl.assert_not_called()

    def test_menu_retains_constructed_status_items_and_clears_them_on_rebuild(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] /
                          'src/resources/lib/oeWindows.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and
                   node.name == 'mainWindow')
        methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and
                   node.name in ('build_menu', 'addConfigItem')]
        namespace = {'xbmcgui': mock.Mock(), 'lang_str': ''}
        namespace['xbmcgui'].ListItem.side_effect = lambda **kwargs: mock.Mock()
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<owned-menu>', 'exec'), namespace)
        window = types.SimpleNamespace(calibration_rows={}, getControl=mock.Mock(),
                                       oe=types.SimpleNamespace(_=str, dbg_log=mock.Mock()))
        window.getControl.return_value.size.side_effect = lambda: len(
            window.getControl.return_value.addItem.call_args_list)
        window.addConfigItem = types.MethodType(namespace['addConfigItem'], window)
        entries = ('CalibrationHealth', 'CalibrationTiming')
        settings = {entry: {'order': index, 'value': '', 'type': 'status', 'action': '',
                            'name': entry} for index, entry in enumerate(entries)}
        namespace['build_menu'](window, {'ethernet': {'order': 0, 'name': 1, 'settings': settings}})
        self.assertEqual(set(window.calibration_rows), set(entries))
        added = [call.args[0] for call in window.getControl.return_value.addItem.call_args_list]
        self.assertTrue(all(item in added for item in window.calibration_rows.values()))
        namespace['build_menu'](window, {'updates': {'order': 0, 'name': 2, 'settings': {}}})
        self.assertEqual(window.calibration_rows, {})
        self.assertEqual(window.calibration_positions, {})
        window.getControl.return_value.getListItem.assert_not_called()

    def test_polling_is_scoped_to_visible_network_settings(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] /
                          'src/resources/lib/modules/connman.py').read_text())
        module = next(node for node in tree.body if isinstance(node, ast.ClassDef) and
                      node.name == 'connman')
        method = next(node for node in module.body if isinstance(node, ast.FunctionDef) and
                      node.name == 'refresh_calibration')
        namespace = {'oe': oe, 'log': log, 'ethernet_calibration': client}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<connman>', 'exec'), namespace)
        window = mock.Mock(visible=True)
        with mock.patch.object(oe, 'winOeMain', window, create=True), \
                mock.patch.object(client, 'refresh_rows') as refresh:
            for rows, visible, expected in (
                    ({'CalibrationHealth': mock.Mock()}, True, True),
                    ({}, True, False), ({'CalibrationHealth': mock.Mock()}, False, False)):
                window.calibration_rows = rows
                window.visible = visible
                stop = mock.Mock()
                stop.wait.side_effect = [False, True]
                namespace['refresh_calibration'](types.SimpleNamespace(
                    visible=True), stop)
                self.assertEqual(refresh.called, expected)
                window.getControl.assert_not_called()
                refresh.reset_mock()

    def test_rapid_reopen_replaces_worker_committed_to_exit(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] /
                          'src/resources/lib/modules/connman.py').read_text())
        module = next(node for node in tree.body if isinstance(node, ast.ClassDef) and
                      node.name == 'connman')
        methods = [node for node in module.body if isinstance(node, ast.FunctionDef) and
                   node.name in ('do_init', 'refresh_calibration', 'exit')]
        stops = []
        class PausedStop:
            def __init__(self):
                self.event = threading.Event()
                self.observed = threading.Event()
                self.release = threading.Event()
                stops.append(self)
            def set(self):
                self.event.set()
            def clear(self):
                self.event.clear()
            def is_set(self):
                return self.event.is_set()
            def wait(self, timeout):
                stopped = self.event.wait(timeout)
                if stopped:
                    self.observed.set()
                    self.release.wait(5)
                return stopped
        namespace = {'oe': oe, 'log': log, 'ethernet_calibration': client,
                     'threading': types.SimpleNamespace(Thread=threading.Thread, Event=PausedStop)}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<connman>', 'exec'), namespace)
        worker = types.SimpleNamespace(visible=False, calibration_lock=threading.Lock(),
                                       calibration_stop=PausedStop(), calibration_thread=None,
                                       clear_list=mock.Mock())
        worker.refresh_calibration = types.MethodType(namespace['refresh_calibration'], worker)
        threads = []
        try:
            namespace['do_init'](worker)
            old_thread, old_stop = worker.calibration_thread, worker.calibration_stop
            threads.append(old_thread)
            namespace['exit'](worker)
            self.assertTrue(old_stop.observed.wait(2), 'Old worker did not observe stop')
            self.assertTrue(old_thread.is_alive())
            namespace['do_init'](worker)
            threads.append(worker.calibration_thread)
            self.assertIsNot(worker.calibration_thread, old_thread)
            self.assertIsNot(worker.calibration_stop, old_stop)
            self.assertTrue(old_stop.is_set())
            self.assertTrue(worker.calibration_thread.is_alive())
        finally:
            namespace['exit'](worker)
            for stop in stops:
                stop.set()
                stop.release.set()
            for thread in threads:
                thread.join(2)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_stopped_worker_does_not_publish_delayed_status_reply(self):
        item = mock.Mock()
        window = types.SimpleNamespace(calibration_rows={'CalibrationHealth': item})
        stop = threading.Event()
        fetched, release = threading.Event(), threading.Event()
        def delayed(**kwargs):
            fetched.set()
            self.assertTrue(release.wait(3))
            return {'CalibrationHealth': 'obsolete'}, 'obsolete help'
        with mock.patch.object(client, 'menu_status', side_effect=delayed):
            thread = threading.Thread(target=client.refresh_rows, args=(window, stop))
            thread.start()
            try:
                self.assertTrue(fetched.wait(2))
                stop.set()
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        item.setProperty.assert_not_called()

    def test_async_reset_cancel_does_not_claim_to_cancel_committed_driver_change(self):
        self.progress_patcher.stop()
        progress_dialog = mock.Mock()
        progress_dialog.iscanceled.return_value = True
        monitor = mock.Mock()
        monitor.abortRequested.return_value = False
        monitor.waitForAbort.return_value = False
        self.request.side_effect = [
            {'phase': 'clearing', 'busy': True}, {'phase': 'cleared', 'busy': False}]
        with mock.patch.object(client.xbmcgui, 'DialogProgress', return_value=progress_dialog,
                               create=True), \
                mock.patch.object(client.xbmc, 'Monitor', return_value=monitor, create=True):
            client.progress()
        self.assertEqual(self.request.call_args_list, [mock.call('status'), mock.call('status')])
        self.dialog.ok.assert_called_with('33620', '33657')

    def test_public_result_does_not_claim_calibration_was_saved(self):
        self.progress_patcher.stop()
        self.request.return_value = {
            'phase': 'diagnostic_complete', 'busy': False,
            'diagnostic': {'state': 'no_errors', 'saved': False, 'gigabit_verified': False}}
        monitor = mock.Mock()
        monitor.abortRequested.return_value = False
        with mock.patch.object(client.xbmcgui, 'DialogProgress', create=True), \
                mock.patch.object(client.xbmc, 'Monitor', return_value=monitor, create=True):
            client.progress()
        self.dialog.ok.assert_called_with('33620', '33664')

    def test_manual_is_available_inside_the_calibration_action_menu(self):
        self.dialog.select.return_value = 3
        self.dialog.yesno.return_value = True
        self.answers(['0'])
        client.open_dialog()
        self.request.assert_called_with('manual', index=0, consent=True)
        self.assertIn('33686', self.dialog.select.call_args.args[1])
        self.assertFalse(any(call.args[0] == 'start' for call in self.request.call_args_list))

    def english(self):
        import json
        import re
        text = (Path(__file__).resolve().parents[1] /
                'language/resource.language.en_gb/strings.po').read_text(encoding='utf-8')
        strings = {int(number): json.loads('"' + value + '"')
                   for number, value in re.findall(r'msgctxt "#(\d+)"\nmsgid "(.*)"', text)}
        return mock.patch.object(client.oe, '_', side_effect=lambda number: strings[number])

    def test_actual_public_pass_and_saved_messages_lead_with_unambiguous_success(self):
        with self.english():
            checked = client.result_message({'phase': 'diagnostic_complete',
                      'diagnostic': {'state': 'no_errors', 'saved': False}})
            self.assertTrue(checked.startswith('Test passed.'))
            self.assertIn('no new calibration was saved', checked)
            self.assertIn('Gigabit speed not measured', checked)
            for backend in ('iperf3', 'cloudflare_https_experimental'):
                saved = client.result_message({'phase': 'public_complete', 'traffic_backend': backend})
                self.assertTrue(saved.startswith('Calibration saved.'))
                self.assertIn('Gigabit speed not measured', saved)
                self.assertNotIn('error checks', saved.lower())

    def test_actual_current_factory_and_saved_manual_indices_are_distinct(self):
        self.status.update(timing={'factory': True, 'index': 0, 'state': 'pending'})
        self.status['profile'].update(kind='manual_unverified', configured_index=31, state='inactive')
        with self.english():
            rows = client.row_values(self.status)
            self.assertEqual(rows['CalibrationTiming'], 'Timing not yet applied')
            info = client.timing_values(self.status)[1]
            self.assertIn('Factory (index 0)', info)
            self.assertIn('Saved timing: index 31.', info)
            self.assertIn('Set manually. Not verified', info)
            self.assertNotIn('green', rows['CalibrationHealth'])
            self.status['timing'].update(factory=False, index=31, state='failed')
            self.assertEqual(client.row_values(self.status)['CalibrationTiming'], 'Timing could not be applied')

    def test_compact_timing_exact_applied_saved_factory_and_temporary_states(self):
        cases = [
            (False, 30, 'applied', 'applied', 30, 'Index 30 - Saved'),
            (False, 12, 'applied', 'inactive', 30, 'Index 12 - Saved: 30'),
            (True, 0, 'applied', 'inactive', 30, 'Factory - Index 0 - Saved: 30'),
            (True, 0, 'applied', 'not_saved', None, 'Factory - Index 0'),
            (False, 0, 'applied', 'not_saved', None, 'Index 0 - Not saved'),
            (False, 12, 'temporary', 'inactive', 30, 'Index 12 - Temporary'),
            (False, 30, 'pending', 'pending', 30, 'Timing not yet applied'),
            (False, 30, 'failed', 'pending', 30, 'Timing could not be applied'),
            (False, 30, 'applied', 'inactive', None, 'Index 30 - Saved timing unavailable'),
        ]
        with self.english():
            for factory, index, state, profile_state, saved, expected in cases:
                with self.subTest(expected=expected):
                    self.status['timing'] = dict(factory=factory, index=index, state=state)
                    self.status['profile'] = dict(state=profile_state, kind='manual_unverified')
                    if saved is not None:
                        self.status['profile']['configured_index'] = saved
                    value, info = client.timing_values(self.status)
                    self.assertEqual(value, expected)
                    self.assertNotIn('\n', value)
                    self.assertNotIn('green', value)
                    if saved is not None:
                        self.assertIn('Saved timing: index %d.' % saved, info)
                        self.assertIn('Not verified for packet errors or gigabit speed', info)
                    if state in ('pending', 'failed', 'temporary'):
                        self.assertNotIn('The saved timing is in use.', info)
                    self.assertEqual(set(client.row_values(self.status)),
                                     {'CalibrationHealth', 'CalibrationTiming'})

    def test_busy_missing_status_and_invalid_saved_values_do_not_invent_applied_timing(self):
        with self.english():
            self.status.update(busy=True)
            self.status.pop('timing')
            self.status['profile'] = {'state': 'not_saved'}
            values, info = client.menu_status(with_help=True)
            self.assertEqual(values['CalibrationTiming'], 'Operation in progress')
            self.assertIn('unavailable during this operation', info)
            self.assertNotIn('Index', values['CalibrationTiming'])
            self.status.update(busy=False, supported=False)
            self.assertEqual(client.row_values(self.status)['CalibrationTiming'],
                             client.oe._(33626))
            self.status.update(supported=True, timing={'factory': False, 'index': 30, 'state': 'applied'})
            for invalid in (True, 32, '30'):
                self.status['profile']['configured_index'] = invalid
                values, info = client.menu_status(with_help=True)
                self.assertEqual(values['CalibrationTiming'], 'Ethernet status unavailable')
                self.assertEqual(info, 'Ethernet status unavailable')
            self.request.side_effect = OSError('offline')
            values, info = client.menu_status(with_help=True)
            self.assertEqual(set(values), {'CalibrationHealth', 'CalibrationTiming'})
            self.assertEqual(info, 'Ethernet status unavailable')

    def test_compact_help_retains_evidence_without_certifying_current_health(self):
        with self.english():
            self.status['last_verification'] = {
                'kind': 'lan_gigabit', 'index': 30, 'send_bps': 939_000_000, 'receive_bps': 941_000_000}
            self.status['health']['state'] = 'insufficient_traffic'
            for kind, backend, evidence in (
                    ('lan_gigabit', '', 'Upload: 939 Mbps. Download: 941 Mbps.'),
                    ('manual_unverified', '', 'Not verified for packet errors or gigabit speed.'),
                    ('public_error_checked', 'iperf3', 'public iPerf packet checks passed'),
                    ('public_error_checked', 'cloudflare_https_experimental', 'experimental HTTPS packet checks passed')):
                self.status['profile'].update(kind=kind, traffic_backend=backend, margin='reduced')
                value, info = client.timing_values(self.status)
                self.assertEqual(value, 'Index 30 - Saved')
                self.assertIn(evidence, info)
                self.assertIn('not current Ethernet health', info)
                self.assertIn(client.oe._(33650), info)
                self.assertNotIn('green', client.row_values(self.status)['CalibrationHealth'])
                if kind != 'lan_gigabit':
                    self.assertNotIn('939', info)
                if kind == 'public_error_checked':
                    self.assertIn('Gigabit speed not measured.', info)
            self.status['profile']['kind'] = 'lan_gigabit'
            self.status['last_verification']['index'] = 31
            self.assertNotIn('939', client.timing_values(self.status)[1])

    def test_focused_help_refresh_uses_owned_items_and_does_not_move_selection(self):
        item = mock.Mock()
        control = mock.Mock()
        control.getSelectedPosition.return_value = 2
        window = mock.Mock(visible=True, calibration_rows={'CalibrationTiming': item},
                           calibration_positions={'CalibrationTiming': (1100, 2)})
        window.getFocusId.return_value = 1100
        window.getControl.return_value = control
        with self.english():
            client.refresh_rows(window)
            info = client.timing_values(self.status)[1]
        item.setProperty.assert_any_call('InfoText', info)
        window.setProperty.assert_called_once_with('InfoText', info)
        item.getProperty.assert_not_called()
        control.getSelectedItem.assert_not_called()
        control.getListItem.assert_not_called()
        control.selectItem.assert_not_called()
        window.build_menu.assert_not_called()
        window.setProperty.reset_mock()
        control.getSelectedPosition.return_value = 3
        client.refresh_rows(window)
        window.setProperty.assert_not_called()
        window.getFocusId.return_value = 1000
        client.refresh_rows(window)
        window.setProperty.assert_not_called()

    def test_timing_focus_navigation_and_ok_use_the_retained_item(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] /
                          'src/resources/lib/oeWindows.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'mainWindow')
        methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and
                   node.name in ('calibration_item', 'onFocus', 'onAction', 'onClick')]
        namespace = {}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<owned-focus>', 'exec'), namespace)
        item, control = mock.Mock(), mock.Mock()
        item.getProperty.side_effect = lambda key: {'typ': 'label', 'action': '', 'InfoText': 'fresh evidence'}.get(key, '')
        control.getSelectedPosition.return_value = 2
        window = mock.Mock(buttons={}, guiLists=[1000, 1100], guiMenList=1000, guiList=1100,
                           lastGuiList=-1, lastMenu=2, calibration_rows={'CalibrationTiming': item},
                           calibration_positions={'CalibrationTiming': (1100, 2)})
        window.oe.CANCEL = []
        window.getFocusId.return_value = 1100
        window.getControl.return_value = control
        window.calibration_item = types.MethodType(namespace['calibration_item'], window)
        namespace['onFocus'](window, 1100)
        window.setProperty.assert_called_with('InfoText', 'fresh evidence')
        window.lastGuiList = -1
        action = mock.Mock()
        action.getId.return_value = 3
        namespace['onAction'](window, action)
        window.setProperty.assert_called_with('InfoText', 'fresh evidence')
        control.selectItem.reset_mock()
        namespace['onClick'](window, 1100)
        self.assertEqual(window.lastMenu, 2)
        control.selectItem.assert_not_called()
        control.getSelectedItem.assert_not_called()
        control.getListItem.assert_not_called()
        window.onFocus.assert_not_called()

    def test_receive_progress_shows_round_exact_payload_and_elapsed_without_eta(self):
        with self.english():
            percent, message = client.progress_values({
                'phase': 'public_verifying', 'candidate': 30, 'verification_round': 2,
                'verification_rounds': 2, 'activity': 'receiving',
                'received_bytes': 12_500_000, 'target_bytes': 25_000_000,
                'traffic_seconds': 10, 'traffic_target_seconds': 20,
                'elapsed_seconds': 91, 'server': 'Cloudflare HTTPS (experimental)'})
        self.assertEqual(percent, 50)
        self.assertIn('Round 2 of 2', message)
        self.assertIn('Index 30', message)
        self.assertIn('Received 12.5 / 25.0 MB of application payload', message)
        self.assertIn('Elapsed: 1:31', message)
        self.assertLessEqual(len(message.splitlines()), 4)
        self.assertNotIn('ETA', message)

    def test_live_recovery_updates_countdown_then_cancel_waits_for_restoration(self):
        self.progress_patcher.stop()
        progress_dialog = mock.Mock()
        progress_dialog.iscanceled.side_effect = [False, True]
        monitor = mock.Mock()
        monitor.abortRequested.return_value = False
        monitor.waitForAbort.return_value = False
        states = iter([
            {'busy': True, 'phase': 'public_recovering', 'candidate': 7, 'activity': 'reconnecting',
             'recovery_remaining_seconds': 19, 'elapsed_seconds': 31},
            {'busy': True, 'phase': 'public_recovering', 'candidate': 7, 'activity': 'reconnecting',
             'recovery_remaining_seconds': 16, 'elapsed_seconds': 34},
            {'busy': False, 'phase': 'cancelled'}])
        self.request.side_effect = lambda action, **kwargs: next(states) if action == 'status' else {
            'cancel_requested': True}
        order = mock.Mock()
        order.attach_mock(progress_dialog.close, 'close')
        order.attach_mock(self.dialog.ok, 'result')
        with self.english(), \
                mock.patch.object(client.xbmcgui, 'DialogProgress', return_value=progress_dialog, create=True), \
                mock.patch.object(client.xbmc, 'Monitor', return_value=monitor, create=True):
            client.progress()
        first, second = [call.args[1] for call in progress_dialog.update.call_args_list]
        self.assertIn('19 seconds remaining', first)
        self.assertIn('Cancelling; restoring previous timing', second)
        self.assertIn('Elapsed: 0:34', second)
        self.assertEqual(sum(call.args[0] == 'cancel' for call in self.request.call_args_list), 1)
        self.assertEqual([call[0] for call in order.mock_calls], ['close', 'result'])
        self.assertIn('saved calibration unchanged', self.dialog.ok.call_args.args[1])

    def test_failure_inconclusive_corruption_and_cancel_never_read_as_success(self):
        with self.english():
            for phase in ('failed', 'inconclusive', 'corruption', 'cancelled'):
                message = client.result_message({'phase': phase, 'error': 'fixture detail'})
                self.assertFalse(message.startswith(('Test passed', 'Calibration saved')))
            failed = client.result_message({'phase': 'failed', 'error': 'restore failed'})
            self.assertIn('Check the timing status', failed)
            self.assertIn('restore failed', failed)

    def test_custom_public_consent_still_requires_permission_for_repair(self):
        self.dialog.select.side_effect = [0, 1, 1]
        self.dialog.yesno.return_value = True
        self.numeric.return_value.result = None
        client.open_dialog()
        self.dialog.yesno.assert_called_once_with('33620', '33643')

    def entry(self, kind=3, default='0.0.0.0'):
        entry = NumericEntry(heading=33644, kind=kind, default=default)
        entry.getControl = mock.Mock()
        entry.close = mock.Mock()
        return entry

    def test_visible_cancel_and_back_both_discard_ip_and_port_entry(self):
        source = ET.parse(Path(__file__).resolve().parents[1] /
                          'skins/Default/1080i/service-CoreELEC-Ethernet-Numeric.xml')
        cancel = source.find('.//control[@id="24"]')
        self.assertEqual(cancel.findtext('label'), '$LOCALIZE[222]')
        self.assertIsNone(cancel.find('visible'))
        for kind, default in ((3, '0.0.0.0'), (0, '5201')):
            for visible in (True, False):
                entry = self.entry(kind, default)
                entry.number(1)
                if visible:
                    entry.onClick(24)
                else:
                    entry.onAction(mock.Mock(getId=mock.Mock(return_value=92)))
                self.assertIsNone(entry.result)
                entry.close.assert_called_once()

    def test_numeric_ip_octets_previous_next_and_delete(self):
        entry = self.entry()
        for index, value in enumerate(('192', '168', '2', '79')):
            for digit in value:
                entry.onClick(10 + int(digit))
            if index < 3:
                entry.onClick(22)
        entry.onClick(23)
        entry.onClick(19)
        entry.onClick(21)
        self.assertEqual(entry.result, '192.168.2.79')
        entry.close.assert_called_once()

    def test_invalid_numeric_values_stay_open_and_can_be_cancelled(self):
        entry = self.entry()
        entry.confirm()
        entry.close.assert_not_called()
        self.assertIsNone(entry.result)
        port = self.entry(0, '5201')
        for digit in '65536':
            port.number(int(digit))
        port.confirm()
        port.close.assert_not_called()
        port.getControl.return_value.setLabel.assert_called_with('33673')
        port.onClick(24)
        self.assertIsNone(port.result)

    def test_public_preset_selection_requires_no_address_or_port_typing(self):
        self.dialog.select.side_effect = [0, 1, 0, 0, 1]
        self.dialog.yesno.return_value = True
        client.open_dialog()
        self.numeric.assert_not_called()
        self.request.assert_called_with('start', preset='scaleway-france',
                                        port=5201, public=True, consent=True)
        self.progress.assert_called_once()
        self.assertIn('Scaleway - France (ping.online.net)',
                      self.dialog.select.call_args_list[3].args[1][0])
        self.assertIn('Moji - France (iperf3.moji.fr)',
                      self.dialog.select.call_args_list[3].args[1][1])

    def test_public_picker_back_starts_no_job_or_dns(self):
        self.dialog.select.side_effect = [0, 1, -1]
        client.open_dialog()
        self.request.assert_called_once_with('status')
        self.numeric.assert_not_called()
        self.dialog.yesno.assert_not_called()

    def test_custom_public_entry_retains_ip_and_fresh_port(self):
        self.dialog.select.side_effect = [0, 1, 1]
        self.dialog.yesno.return_value = True
        self.answers(['1.1.1.1', '5201'])
        client.open_dialog()
        self.request.assert_called_with('start', address='1.1.1.1', port=5201,
                                        public=True, consent=True)

    def test_consent_describes_measured_duration_without_changing_restore_warning(self):
        self.dialog.select.side_effect = [0, 0]
        self.dialog.yesno.return_value = False
        client.open_dialog()
        self.dialog.yesno.assert_called_once_with('33620', '33667')
        self.dialog.reset_mock()
        self.dialog.select.side_effect = None
        self.dialog.select.return_value = 1
        client.open_dialog()
        self.dialog.yesno.assert_called_once_with('33620', '33639')
        source = (Path(__file__).resolve().parents[1] /
                  'language/resource.language.en_gb/strings.po').read_text()
        self.assertIn('20 seconds of traffic plus connection setup', source)
        self.assertIn('The time budget is 25 minutes', source)
        self.assertNotIn('usually takes about', source)

    def test_directory_host_and_port_back_do_not_start_tests(self):
        for selections in ([0, 1, 0, -1], [0, 1, 0, 0, -1]):
            self.request.reset_mock()
            self.dialog.select.side_effect = selections
            client.open_dialog()
            self.assertFalse(any(call.args[0] == 'start' for call in self.request.call_args_list))
        self.progress.assert_not_called()

    def test_unsupported_directory_entry_is_visible_and_explained_without_testing(self):
        self.status['public_servers'][0]['unsupported_reason'] = 'IPv6-only server'
        self.dialog.select.side_effect = [0, 1, 0, 0]
        client.open_dialog()
        self.dialog.ok.assert_called_with('33620', 'IPv6-only server')
        self.assertIn('33679', self.dialog.select.call_args.args[1][0])
        self.progress.assert_not_called()
        self.numeric.assert_not_called()

    def test_port_range_selection_and_stale_status_are_not_silent_fallback(self):
        self.status['public_servers'][1]['status'] = 'OOS 10/2026'
        self.dialog.select.side_effect = [0, 1, 0, 1, 40]
        self.dialog.yesno.return_value = True
        client.open_dialog()
        self.request.assert_called_with('start', preset='iperf3.moji.fr', port=5240,
                                        public=True, consent=True)
        self.assertIn('OOS 10/2026', self.dialog.yesno.call_args.args[1])
        self.assertIn('2026-11-22', self.dialog.yesno.call_args.args[1])
        self.assertEqual(self.dialog.select.call_args.args[1], [str(port) for port in range(5200, 5241)])

    def test_region_pagination_includes_every_host_in_selection(self):
        template = self.status['public_servers'][0]
        self.status['public_servers'] = [
            dict(template, id='server-' + str(index), hostname='host' + str(index))
            for index in range(10)]
        self.dialog.select.side_effect = [0, 1, 0, 9, 1]
        self.dialog.yesno.return_value = True
        client.open_dialog()
        self.assertEqual(sum(call.args[0] == 'catalogue' for call in self.request.call_args_list), 2)
        self.assertEqual(len(self.dialog.select.call_args_list[3].args[1]), 10)
        self.request.assert_called_with('start', preset='server-9', port=5201, public=True, consent=True)

    def test_public_saved_profile_is_explicitly_not_a_gigabit_speed_result(self):
        self.status['profile']['kind'] = 'public_error_checked'
        self.status['last_verification'] = {'kind': 'public_error_checked'}
        rows = client.row_values(self.status)
        self.assertIn('33756', client.timing_values(self.status)[1])
        self.assertNotIn('[COLOR green]', rows['CalibrationHealth'])

    def test_public_repair_completion_displays_reduced_evidence_not_lan_success(self):
        self.progress_patcher.stop()
        self.request.return_value = {'phase': 'public_complete', 'busy': False}
        monitor = mock.Mock()
        monitor.abortRequested.return_value = False
        with mock.patch.object(client.xbmcgui, 'DialogProgress', create=True), \
                mock.patch.object(client.xbmc, 'Monitor', return_value=monitor, create=True):
            client.progress()
        self.dialog.ok.assert_called_with('33620', '33682')

    def click_handler(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] /
                          'src/resources/lib/oeWindows.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and
                   node.name == 'mainWindow')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and
                      node.name == 'onClick')
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<oeWindows>', 'exec'), namespace)
        return namespace['onClick']

    def test_status_row_ok_preserves_focus_selection_and_loaded_menu(self):
        handler = self.click_handler()
        for entry in ('CalibrationHealth', 'CalibrationTiming'):
            with self.subTest(entry=entry):
                window = mock.Mock(buttons={}, guiLists=[1100], guiMenList=1000, lastMenu=2)
                item = window.getControl.return_value.getSelectedItem.return_value
                item.getProperty.side_effect = {'typ': 'label', 'action': '', 'entry': entry}.get
                window.calibration_item.return_value = item
                handler(window, 1100)
                self.assertEqual(window.lastMenu, 2)
                window.onFocus.assert_not_called()
                window.setFocusId.assert_not_called()
                window.getControl.return_value.selectItem.assert_not_called()
                window.emptyButtonLabels.assert_not_called()
                window.getControl.return_value.getSelectedItem.assert_not_called()

    def test_action_button_keeps_existing_execution_and_refresh(self):
        window = mock.Mock(buttons={}, guiLists=[1100], guiMenList=1000, lastMenu=2)
        window.calibration_item.return_value = None
        item = window.getControl.return_value.getSelectedItem.return_value
        item.getProperty.side_effect = {'typ': 'button', 'action': 'calibrate',
                                       'modul': 'connman'}.get
        module = mock.Mock()
        window.oe.dictModules = {'connman': module}
        self.click_handler()(window, 1100)
        module.calibrate.assert_called_once_with(listItem=item)
        window.onFocus.assert_called_once_with(1000)

    def test_service_failure_is_reported_not_treated_as_success(self):
        self.request.side_effect = OSError('socket unavailable')
        client.open_dialog()
        self.dialog.ok.assert_called_once()
        self.progress.assert_not_called()


if __name__ == '__main__':
    unittest.main()
