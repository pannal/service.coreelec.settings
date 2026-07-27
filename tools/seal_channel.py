#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)

"""Seal a Mega folder link under a password for the internal update channel.

Repository tooling. This is not shipped in the image and never runs on a box.

    tools/seal_channel.py 'https://mega.nz/folder/<id>#<key>'

Prompts for the password twice, prints one base64 line, and that line goes into
INTERNAL_BLOB in src/resources/lib/updates/channels.py.

Rotating the folder means running this again and shipping an addon update. The
password is never stored anywhere - if it is lost, reseal with a new one.

Needs pycryptodome on the machine you run it on (it ships in the image, but this
script runs on your build host).
"""

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'resources', 'lib'))

from updater import channels          # noqa: E402
from updater import mega_client        # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('link', help='Mega folder link, including the #key fragment')
    parser.add_argument('--password', help='skip the prompt (leaks into shell history)')
    args = parser.parse_args()

    try:
        mega_client.parse_folder_link(args.link)
    except ValueError as exc:
        parser.error(f'{exc}: expected https://mega.nz/folder/<8 chars>#<22 chars>')

    # 'is None', not falsiness: --password '' means "the user asked for an
    # empty password", which must be refused rather than sent to the prompt.
    password = args.password
    if password is None:
        password = getpass.getpass('Password for the internal channel: ')
        if password != getpass.getpass('Repeat: '):
            sys.exit('Passwords did not match.')
    if not password:
        sys.exit('Refusing to seal with an empty password.')

    blob = channels.seal(args.link, password)

    # Prove the blob opens before handing it over.
    if channels.unseal(blob, password) != args.link:
        sys.exit('Internal error: sealed blob did not round-trip.')

    print()
    print('Paste this into INTERNAL_BLOB in')
    print('  src/resources/lib/updater/channels.py')
    print()
    print(f"INTERNAL_BLOB = '{blob}'")
    print()


if __name__ == '__main__':
    main()
