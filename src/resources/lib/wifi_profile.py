# SPDX-License-Identifier: GPL-2.0-or-later
"""Persist the optional driver profile without changing Wi-Fi connections."""

import os
from pathlib import Path
import tempfile


SDIO_DEVICES = Path('/sys/bus/sdio/devices')
CONFIG_FILE = Path('/storage/.config/modprobe.d/99-p3i-wifi-throughput.conf')
OPTION = 'options dhd_sdio p3i_tput_profile='


def active_value():
    """Return the effective profile only for supported hardware and driver."""
    for device in sorted(SDIO_DEVICES.glob('*')):
        try:
            if int((device / 'vendor').read_text().strip(), 16) != 0x02d0:
                continue
            if int((device / 'device').read_text().strip(), 16) != 0xaae8:
                continue
            # Follow the bound module; its name need not match its .ko filename.
            value = (device / 'driver/module/parameters/p3i_tput_active').read_text().strip()
            if value in ('0', '1'):
                return value
        except (OSError, ValueError):
            continue
    return None


def saved_value(active):
    """Show a saved reboot-pending choice, otherwise the effective value."""
    try:
        content = CONFIG_FILE.read_text().strip()
    except FileNotFoundError:
        return active
    for value in ('0', '1'):
        if content == OPTION + value:
            return value
    raise ValueError('Invalid saved Wi-Fi throughput setting')


def save_value(value):
    if value not in ('0', '1'):
        raise ValueError('Invalid Wi-Fi throughput value')
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=CONFIG_FILE.parent,
                                         prefix='.p3i-wifi-', delete=False) as output:
            temporary = Path(output.name)
            output.write(OPTION + value + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, CONFIG_FILE)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
