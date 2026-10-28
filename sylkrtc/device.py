# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""Who this computer is to SylkServer, the same for every tool of the package.

The server keeps the list of the devices that use an account (its account storage,
namespace devices): the tools (sylk-account, sylk-journal, sylk-session,
sylk-conference, sylk-addressbook) are one device, python-sylkrtc on this host,
with one id, whether they come over HTTPS (X-Sylk-Device-Id, X-Sylk-Device-Name)
or over the WebSocket (account-add deviceId, deviceName), and one User-Agent.

The id is a general setting, SylkSettings.device_id in ~/.sylkclient/config
(sylk-settings --general show), made at the first use of any tool. The file
~/.sylkclient/device_id that sylk-account kept before is taken over once, so its
entry on the server carries on.
"""

import fcntl
import logging
import os
import socket
import uuid

from sylkrtc import __version__


__all__ = ['USER_AGENT', 'device_id', 'device_name']

log = logging.getLogger('sylkrtc.device')

USER_AGENT = f'python-sylkrtc {__version__}'

DIRECTORY = os.path.expanduser('~/.sylkclient')
LEGACY_ID_FILE = 'device_id'        # sylk-account's, before the id was a general setting

_device_id = None


def device_name():
    return f'python-sylkrtc on {socket.gethostname().split(".")[0]}'


def device_id():
    """SylkSettings.device_id of ~/.sylkclient/config, made (and saved there) at the first use of
    any tool. Made under a lock: two tools starting together end up with the same id."""
    global _device_id
    if _device_id is not None:
        return _device_id
    from sipsimple.configuration.backend import ConfigurationBackendError
    from sipsimple.configuration.backend.file import FileBackend
    path = os.path.join(DIRECTORY, 'config')
    try:
        os.makedirs(DIRECTORY, exist_ok=True)
        with open(os.path.join(DIRECTORY, '.device_id.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            backend = FileBackend(path)
            data = backend.load()
            general = data.setdefault('SylkSettings', {})
            value = str(general.get('device_id') or '').strip()
            if value in ('', 'None'):
                value = _legacy_id() or str(uuid.uuid4())
                general['device_id'] = value
                backend.save(data)
                log.info(f'device id {value} kept in {path}')
    except (OSError, ConfigurationBackendError) as e:
        # not kept: this run is a device of its own, the next one tries again
        value = _legacy_id() or str(uuid.uuid4())
        log.warning(f'cannot keep the device id in {path}: {e}')
    _device_id = value
    return value


def _legacy_id():
    try:
        with open(os.path.join(DIRECTORY, LEGACY_ID_FILE)) as file:
            return file.read().strip() or None
    except OSError:
        return None
