# SPDX-License-Identifier: GPL-2.0-or-later
"""Kodi client for the OS-owned, local Ethernet calibration service."""

import ipaddress
import json
import socket

import xbmc
import xbmcgui
import log
import oe
import ui_tools

SOCKET_PATH = '/run/ethernet-calibration/control'
TITLE = 33620
HEALTH = {'healthy': 33621, 'corruption': 33622, 'unknown': 33623,
          'insufficient_traffic': 33624, 'link_down': 33625,
          'unsupported': 33626, 'calibrating': 33627, 'reconnecting': 33662}
PHASE = {'starting': 33627, 'scanning': 33632, 'verifying_send': 33633,
         'verifying_receive': 33634, 'saving': 33635,
         'clearing': 33656, 'restoring': 33658, 'verification_failed': 33660,
         'diagnosing': 33663, 'resolving': 33674,
         'public_scanning': 33683, 'public_verifying': 33684,
         'public_recovering': 33685, 'manual_saving': 33691}


def request(action, **kwargs):
    payload = dict(kwargs, version=1, action=action)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(3)
        client.connect(SOCKET_PATH)
        client.sendall(json.dumps(payload).encode())
        message = client.recv(8193)
    if len(message) > 8192:
        raise ValueError('Calibration service response is too large')
    response = json.loads(message)
    if not isinstance(response, dict):
        raise ValueError(oe._(33647))
    if response.get('version') != 1 or response.get('ok') is not True:
        raise ValueError(response.get('error', oe._(33647)))
    return response


def timing_values(status):
    if status.get('busy'):
        return oe._(33742), oe._(33759)
    if status.get('supported') is False:
        return oe._(33626), oe._(33626)
    timing = status.get('timing')
    if timing is None:
        return oe._(33647), oe._(33647)
    index, state = timing['index'], timing['state']
    if type(index) is not int or not 0 <= index <= 31 or type(timing['factory']) is not bool:
        raise ValueError('Invalid Ethernet timing status')
    current = oe._(33712 if timing['factory'] else 33711) % index
    profile = status['profile']
    saved = profile.get('configured_index')
    if saved is not None and (type(saved) is not int or not 0 <= saved <= 31):
        raise ValueError('Invalid saved Ethernet timing')
    if state == 'pending':
        value, info = oe._(33738), oe._(33744) % current
    elif state == 'failed':
        value, info = oe._(33739), oe._(33745) % current
    elif state == 'temporary':
        value, info = oe._(33737) % index, oe._(33746) % current
    elif state == 'applied':
        info = oe._(33743) % current
        if saved is None:
            value = oe._(33734) % index if timing['factory'] else oe._(33740) % index
            if profile['state'] != 'not_saved':
                value = oe._(33741) % current
        elif profile['state'] == 'applied' and saved == index and not timing['factory']:
            value = oe._(33733) % index
        else:
            value = oe._(33736 if timing['factory'] else 33735) % (index, saved)
    else:
        raise ValueError('Invalid Ethernet application state')
    if saved is None:
        info += '\n' + oe._(33748 if profile['state'] == 'not_saved' else 33749)
    else:
        info += '\n' + oe._(33747) % saved
        applied = state == 'applied' and profile['state'] == 'applied' and saved == index and not timing['factory']
        info += '\n' + oe._(33750 if applied else 33752 if state in ('pending', 'failed') else 33751)
        kind = profile.get('kind', 'lan_gigabit')
        verified = status.get('last_verification')
        if kind == 'manual_unverified':
            info += '\n' + oe._(33753)
        elif kind == 'public_error_checked':
            info += '\n' + oe._(33757 if profile.get('traffic_backend') ==
                                      'cloudflare_https_experimental' else 33756)
        elif kind == 'lan_gigabit':
            if verified and verified.get('kind', 'lan_gigabit') == kind and verified.get('index', saved) == saved and (
                    'send_bps' in verified and 'receive_bps' in verified):
                info += '\n' + oe._(33755) % (verified['send_bps'] / 1_000_000,
                                              verified['receive_bps'] / 1_000_000)
            else:
                info += '\n' + oe._(33754)
        if profile.get('margin') == 'reduced':
            info += '\n' + oe._(33650)
        info += '\n' + oe._(33758)
    if status.get('application_error'):
        info += '\n' + oe._(33655)
    return value, info


def row_values(status):
    state = status['health']['state']
    color = ('red' if state == 'corruption' or status.get('application_error') else
             'green' if state == 'healthy' else 'orange')
    health = oe._(33655) if status.get('application_error') else oe._(HEALTH.get(state, 33623))
    current, _ = timing_values(status)
    return {'CalibrationHealth': '[COLOR %s]%s[/COLOR]' % (color, health),
            'CalibrationTiming': current}


def menu_status(with_help=False):
    try:
        status = request('status')
        values, info = row_values(status), timing_values(status)[1]
    except (OSError, ValueError, KeyError, TypeError) as error:
        log.log('Ethernet calibration status unavailable: %s' % error, log.ERROR)
        values = {name: oe._(33647) for name in ('CalibrationHealth', 'CalibrationTiming')}
        info = oe._(33647)
    return (values, info) if with_help else values


def refresh_rows(window, stop=None):
    rows = window.calibration_rows
    if not rows or stop is not None and stop.is_set():
        return
    values, info = menu_status(with_help=True)
    if window.calibration_rows is not rows or stop is not None and stop.is_set():
        return
    # Retain constructed items, never borrowed native wrappers from a resettable list.
    for entry, item in tuple(rows.items()):
        item.setProperty('value', values[entry])
        if entry == 'CalibrationTiming':
            item.setProperty('InfoText', info)
    position = window.calibration_positions.get('CalibrationTiming')
    if position is not None and window.visible and window.getFocusId() == position[0]:
        control = window.getControl(position[0])
        selected = control.getSelectedPosition()
        if selected == position[1] and window.calibration_rows is rows and (
                stop is None or not stop.is_set()):
            window.setProperty('InfoText', info)


class NumericEntry(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self.heading = kwargs['heading']
        self.kind = kwargs['kind']
        self.value = kwargs['default']
        self.octets = self.value.split('.') if self.kind == 3 else []
        self.block = 0
        self.dirty = False
        self.result = None
        self.minimum = kwargs.get('minimum', 1)
        self.maximum = kwargs.get('maximum', 65535)
        self.error = 33693 if (self.minimum, self.maximum) == (0, 31) else 33673

    def onInit(self):
        self.getControl(1).setLabel(oe._(self.heading))
        self.getControl(20).setVisible(self.kind == 3)
        self.getControl(22).setVisible(self.kind == 3)
        for identifier, label in ((20, 33670), (22, 33671), (23, 33672)):
            self.getControl(identifier).setLabel(oe._(label))
        self.render()

    def render(self):
        text = self.value
        if self.kind == 3:
            text = '.'.join('[COLOR yellow]%s[/COLOR]' % value if index == self.block else value
                            for index, value in enumerate(self.octets))
        self.getControl(4).setLabel(text)

    def number(self, digit):
        self.getControl(5).setLabel('')
        if self.kind == 3:
            value = int(self.octets[self.block]) * 10 + digit if self.dirty else digit
            if value > 255:
                self.getControl(5).setLabel(oe._(33673))
                return
            self.octets[self.block] = str(value)
            self.dirty = True
        else:
            text = self.value + str(digit) if self.dirty else str(digit)
            if len(text) > 5:
                self.getControl(5).setLabel(oe._(33673))
                return
            self.value = text
            self.dirty = True
        self.render()

    def move(self, direction):
        if self.kind == 3:
            self.block = max(0, min(3, self.block + direction))
            self.dirty = False
            self.render()

    def delete(self):
        if self.kind == 3:
            if not self.dirty and self.block:
                self.block -= 1
            self.octets[self.block] = self.octets[self.block][:-1] or '0'
        else:
            self.value = self.value[:-1]
        self.dirty = True
        self.getControl(5).setLabel('')
        self.render()

    def confirm(self):
        value = '.'.join(self.octets) if self.kind == 3 else self.value
        try:
            if self.kind == 3:
                address = ipaddress.IPv4Address(value)
                if address.is_unspecified:
                    raise ValueError('Unspecified address')
            elif not value.isdecimal() or not self.minimum <= int(value) <= self.maximum:
                raise ValueError('Invalid numeric value')
        except ValueError:
            self.getControl(5).setLabel(oe._(self.error))
            return
        self.result = value
        self.close()

    def onClick(self, identifier):
        if 10 <= identifier <= 19:
            self.number(identifier - 10)
        elif identifier == 20:
            self.move(-1)
        elif identifier == 22:
            self.move(1)
        elif identifier == 23:
            self.delete()
        elif identifier == 21:
            self.confirm()
        elif identifier == 24:
            self.result = None
            self.close()

    def onAction(self, action):
        identifier = action.getId()
        if identifier in (9, 10, 92):
            self.result = None
            self.close()
        elif 58 <= identifier <= 67:
            self.number(identifier - 58)
        elif identifier == 110:
            self.delete()
        elif identifier == 135:
            self.confirm()
        elif identifier == 14:
            self.move(1)
        elif identifier == 15:
            self.move(-1)


def prompt(heading, input_type, default, minimum=1, maximum=65535):
    bounds = {} if (minimum, maximum) == (1, 65535) else dict(minimum=minimum, maximum=maximum)
    dialog = NumericEntry('service-CoreELEC-Ethernet-Numeric.xml', oe.__cwd__, 'Default',
                          heading=heading, kind=input_type, default=default, **bounds)
    dialog.doModal()
    return dialog.result


def result_message(state):
    phase = state['phase']
    if phase == 'complete':
        text = oe._(33646)
        if state.get('profile', {}).get('margin') == 'reduced':
            text += '\n' + oe._(33650)
        return text
    if phase == 'public_complete':
        return oe._(33705 if state.get('traffic_backend',
                    state.get('profile', {}).get('traffic_backend')) ==
                    'cloudflare_https_experimental' else 33682)
    if phase == 'manually_set':
        return oe._(33692) % state['index']
    if phase in ('cleared', 'restored', 'cancelled'):
        return oe._({'cleared': 33657, 'restored': 33661, 'cancelled': 33648}[phase])
    if phase == 'diagnostic_complete':
        return oe._({'no_errors': 33664, 'corruption': 33665,
                     'inconclusive': 33666}[state['diagnostic']['state']])
    if phase in ('inconclusive', 'corruption', 'failed'):
        text = oe._({'inconclusive': 33729, 'corruption': 33731, 'failed': 33730}[phase])
        if state.get('error'):
            text += '\n' + state['error']
        return text
    return oe._(33647)


def progress_values(state, cancelled=False):
    phase = state['phase']
    lines = [oe._(PHASE.get(phase, 33627))]
    percent = 0
    if type(state.get('candidate')) is int:
        if 'candidate_number' in state:
            lines.append(oe._(33721) % (state['candidate'], state['candidate_number'],
                                       state['candidate_total']))
        else:
            lines.append(oe._(33711) % state['candidate'])
    if 'verification_round' in state:
        lines[0] += ' / ' + oe._(33651) % (state['verification_round'], state['verification_rounds'])
    activity = state.get('activity')
    if activity == 'reconnecting':
        remaining = state.get('recovery_remaining_seconds')
        lines.append(oe._(33724) % max(0, int(remaining + .999)) if remaining is not None
                     else oe._(33662))
    elif activity in ('applying', 'connecting'):
        lines.append(oe._(33723 if activity == 'applying' else 33725))
    if 'target_bytes' in state and state['target_bytes'] > 0:
        lines.append(oe._(33726) % (state['received_bytes'] / 1_000_000,
                                   state['target_bytes'] / 1_000_000))
        percent = min(99, int(100 * state['received_bytes'] / state['target_bytes']))
    if activity in ('traffic', 'receiving'):
        traffic = oe._(33727) % (int(state.get('traffic_seconds', 0)), state['traffic_target_seconds'])
        if 'target_bytes' in state:
            lines[-1] += ' / ' + traffic
        else:
            lines.append(traffic)
    elapsed = max(0, int(state.get('elapsed_seconds', 0)))
    lines.append(oe._(33722) % (elapsed // 60, elapsed % 60))
    if state.get('server'):
        lines[-1] += ' / ' + state['server']
    if cancelled:
        lines = [oe._(33728), lines[-1]]
    return percent, '\n'.join(lines)


def progress():
    dialog = xbmcgui.DialogProgress()
    dialog.create(oe._(TITLE), oe._(33627))
    monitor = xbmc.Monitor()
    cancelled = False
    closed = False
    try:
        while not monitor.abortRequested():
            state = request('status')
            if not state['busy']:
                dialog.close()
                closed = True
                xbmcgui.Dialog().ok(oe._(TITLE), result_message(state))
                return
            can_cancel = state['phase'] not in ('clearing', 'restoring', 'manual_saving')
            if dialog.iscanceled() and not cancelled and can_cancel:
                request('cancel')
                cancelled = True
            percent, message = progress_values(state, cancelled)
            dialog.update(percent, message)
            if monitor.waitForAbort(0.25):
                if can_cancel:
                    request('cancel')
                return
        request('cancel')
    finally:
        if not closed:
            dialog.close()


@log.log_function()
def open_manual_dialog():
    try:
        status = request('status')
        if status['busy']:
            progress()
            return
        if not status['supported']:
            xbmcgui.Dialog().ok(oe._(TITLE), oe._(33626))
            return
        current = status.get('timing', {}).get('index', status['profile'].get('index'))
        default = str(current) if type(current) is int and 0 <= current <= 31 else ''
        text = prompt(33688, 0, default, minimum=0, maximum=31)
        if text is None:
            return
        index = int(text)
        if not 0 <= index <= 31:
            raise ValueError(oe._(33693))
        if not xbmcgui.Dialog().yesno(oe._(33686), oe._(33687)):
            return
        request('manual', index=index, consent=True)
        progress()
    except (OSError, ValueError, KeyError, TypeError) as error:
        log.log('Manual Ethernet timing failed: %s' % error, log.ERROR)
        xbmcgui.Dialog().ok(oe._(TITLE), str(error))


@log.log_function()
def open_dialog():
    try:
        status = request('status')
        if status['busy']:
            progress()
            return
        if not status['supported']:
            xbmcgui.Dialog().ok(oe._(TITLE), oe._(33626))
            return
        dialog = xbmcgui.Dialog()
        action = dialog.select(oe._(TITLE),
                               [oe._(33636), oe._(33637), oe._(33638), oe._(33686)])
        if action < 0:
            return
        if action == 3:
            open_manual_dialog()
            return
        if action in (1, 2):
            if not dialog.yesno(oe._(TITLE), oe._(33639)):
                return
            request('restore' if action == 1 else 'clear')
            progress()
            return
        mode = dialog.select(oe._(33640), [oe._(33641), oe._(33642), oe._(33700)])
        if mode < 0:
            return
        if mode == 2:
            if not dialog.yesno(oe._(TITLE), oe._(33703)):
                return
            request('start', public=True, consent=True, backend='cloudflare_https_experimental')
            progress()
            return
        public = mode == 1
        preset = None
        selected_port = None
        server = None
        if public:
            catalogue = status['public_catalogue']
            regions = catalogue['regions']
            region = dialog.select(oe._(33675) % catalogue['source_date'],
                                   regions + [oe._(33669)])
            if region < 0:
                return
            if region < len(regions):
                servers = []
                offset = 0
                while offset is not None:
                    page = request('catalogue', region=regions[region], offset=offset)
                    servers.extend(page['servers'])
                    following = page['next_offset']
                    if following is not None and (type(following) is not int or following <= offset):
                        raise ValueError('Invalid public directory page')
                    offset = following
                labels = [entry['label'] + ' (' + entry['hostname'] + ') - ' +
                          (oe._(33679) if entry['unsupported_reason'] else entry['status'])
                          for entry in servers]
                selected = dialog.select(oe._(33668), labels)
                if selected < 0:
                    return
                server = servers[selected]
                if server['unsupported_reason']:
                    dialog.ok(oe._(TITLE), server['unsupported_reason'])
                    return
                preset = server['id']
                ports = list(range(server['port_first'], server['port_last'] + 1))
                if len(ports) > 1:
                    selected = dialog.select(oe._(33676), [str(port) for port in ports],
                                             preselect=ports.index(server['port']))
                    if selected < 0:
                        return
                    selected_port = ports[selected]
                else:
                    selected_port = ports[0]
        message = oe._(33643 if public else 33667)
        if server is not None:
            message += '\n' + (oe._(33678) % status['public_catalogue']['source_date'])
            if server['status'].startswith('OOS'):
                message += '\n' + (oe._(33677) % server['status'])
        if not dialog.yesno(oe._(TITLE), message):
            return
        if preset is not None:
            request('start', preset=preset, port=selected_port, public=True, consent=True)
            progress()
            return
        address = prompt(33644, 3, '0.0.0.0')
        if not address:
            return
        port = prompt(33645, 0, '5201')
        if not port:
            return
        request('start', address=address, port=int(port), public=public, consent=public)
        progress()
    except (OSError, ValueError, KeyError, TypeError) as error:
        log.log('Ethernet calibration request failed: %s' % error, log.ERROR)
        xbmcgui.Dialog().ok(oe._(TITLE), str(error))
