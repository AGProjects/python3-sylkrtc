# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""The accounts of a SIP SIMPLE configuration, read only.

The sylk clients (~/.sylkclient/config, managed with sylk-settings), the SIP SIMPLE
clients (~/.sipclient/config) and Blink (~/.blink/config) keep their accounts in the
same file format, the SDK's file backend. Read without starting the SIP engine; of
the other clients' only the credentials are used, and nothing is written there.
"""

import os
import re

from sipsimple.configuration.backend import ConfigurationBackendError
from sipsimple.configuration.backend.file import FileBackend


__all__ = ['DEFAULT_DIRECTORY', 'SEARCH_DIRECTORIES', 'ConfigError', 'find_directory', 'load', 'Account', 'accounts', 'find_account', 'locate_account', 'api_token', 'SylkConfiguration']


DEFAULT_DIRECTORY = os.path.expanduser('~/.sylkclient')
SEARCH_DIRECTORIES = [DEFAULT_DIRECTORY, os.path.expanduser('~/.sipclient'), os.path.expanduser('~/.blink')]


def find_directory():
    """The first of SEARCH_DIRECTORIES that has a config file; DEFAULT_DIRECTORY when none has."""
    for directory in SEARCH_DIRECTORIES:
        if os.path.isfile(os.path.join(directory, 'config')):
            return directory
    return DEFAULT_DIRECTORY


class ConfigError(Exception):
    pass


def load(directory=DEFAULT_DIRECTORY):
    """The configuration in <directory>/config as nested dicts ({} when there is none)."""
    try:
        return FileBackend(os.path.join(directory, 'config')).load()
    except ConfigurationBackendError as e:
        raise ConfigError(str(e))


def _bool(value, default):
    if value is None:
        return default
    return str(value).lower() in ('true', 'yes', 'on', '1')


class Account(object):
    def __init__(self, id, data, settings):
        self.id = id
        self.username, _, self.domain = id.partition('@')
        self.enabled = _bool(data.get('enabled'), False)
        self.display_name = data.get('display_name') or ''
        auth = data.get('auth') or {}
        self.auth_username = auth.get('username') or self.username
        self.password = auth.get('password') or ''
        tls = settings.get('tls') or {}
        self.verify_tls = _bool(tls.get('verify_server'), False)
        self.instance_id = settings.get('instance_id') or None
        self.user_agent = settings.get('user_agent') or None

    def __repr__(self):
        return f'<Account {self.id}>'


def accounts(directory=DEFAULT_DIRECTORY):
    """(accounts, default account id) of the configuration; Bonjour is left out."""
    data = load(directory)
    settings = data.get('SIPSimpleSettings') or {}
    found = [Account(id, entry or {}, settings) for id, entry in sorted((data.get('Accounts') or {}).items())
             if id != 'bonjour@local' and re.match(r'^[^@\s]+@[^@\s]+$', id)]
    return found, settings.get('default_account')


def find_account(name=None, directory=DEFAULT_DIRECTORY):
    """The account given by name (its address, or the start of it), else the default one, else the
    first enabled one; ConfigError when there is none."""
    found, default = accounts(directory)
    if not found:
        raise ConfigError(f'no accounts in {os.path.join(directory, "config")}')
    if name:
        exact = [account for account in found if account.id == name]
        if exact:
            return exact[0]
        partial = [account for account in found if account.id.startswith(name)]
        if len(partial) == 1:
            return partial[0]
        raise ConfigError(f'account {name} not found' if not partial else f'account {name} is ambiguous: {", ".join(a.id for a in partial)}')
    for account in found:
        if account.id == default and account.enabled:
            return account
    for account in found:
        if account.enabled:
            return account
    raise ConfigError('no enabled account')


def locate_account(name=None, directory=None):
    """(account, directory) of the account to use, searched in directory when one is given, else in every
    configuration in the order of SEARCH_DIRECTORIES (~/.sylkclient, ~/.sipclient, ~/.blink):

    - name given: its exact address in the first configuration that has it, else the only account
      (in all of them) whose address contains it;
    - no name: the default account of the first configuration that has a usable one (enabled, with a
      password), else the first enabled account with a password.
    """
    directories = [directory] if directory is not None else [candidate for candidate in SEARCH_DIRECTORIES if os.path.isfile(os.path.join(candidate, 'config'))]
    if not directories:
        raise ConfigError(f'no configuration in {", ".join(SEARCH_DIRECTORIES)} (add an account with sylk-settings --account add, '
                          f'or import one with sylk-settings --account import)')
    found = []      # (account, directory, default account id), in search order
    for candidate in directories:
        found_here, default = accounts(candidate)
        found += [(account, candidate, default) for account in found_here]
    if not found:
        raise ConfigError(f'no accounts in {", ".join(os.path.join(candidate, "config") for candidate in directories)}')
    if name:
        for account, candidate, default in found:
            if account.id == name:
                return account, candidate
        partial = {account.id: (account, candidate) for account, candidate, default in reversed(found) if name in account.id}
        if len(partial) == 1:
            return next(iter(partial.values()))
        raise ConfigError(f'account {name} not found' if not partial else f'account {name} is ambiguous: {", ".join(sorted(partial))}')
    usable = [(account, candidate, default) for account, candidate, default in found if account.enabled and account.password]
    for account, candidate, default in usable:
        if account.id == default:
            return account, candidate
    if usable:
        return usable[0][:2]
    raise ConfigError('no enabled account with a password')


def api_token(account_id):
    """(token, config file) of the SylkServer API token a client keeps for the account, or (None, None).

    The token is the one application/sylk-api-token hands out over SIP (Blink keeps it as
    sms.history_synchronization_token). The gateway does not hand it to a WebSocket client, so a
    sylk client borrows it from another client's configuration, in the order of SEARCH_DIRECTORIES.
    """
    for directory in SEARCH_DIRECTORIES:
        path = os.path.join(directory, 'config')
        if not os.path.isfile(path):
            continue
        try:
            entry = (load(directory).get('Accounts') or {}).get(account_id) or {}
        except ConfigError:
            continue
        token = (entry.get('sms') or {}).get('history_synchronization_token')
        if token and token != 'None':
            return str(token), path
    return None, None


# The sylk clients' own settings (written with sylkrtc.settings / sylk-settings), read without the SDK

def _text(value):
    if isinstance(value, list):
        value = ', '.join(value)
    return value if value not in (None, '') else None


def _names(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    names = [name.strip() for name in value if name and name.strip()]
    return names or None


class SylkConfiguration(object):
    """General settings, sessions, RTSP and local cameras of ~/.sylkclient/config (see sylkrtc.settings)."""

    def __init__(self, directory=DEFAULT_DIRECTORY):
        data = load(directory)
        general = data.get('SylkSettings') or {}
        self.audio_input = _text(general.get('audio_input')) or 'auto'
        self.audio_output = _text(general.get('audio_output')) or 'auto'
        self.muted = _bool(general.get('muted'), False)
        self.tls_verify_server = _bool(general.get('tls_verify_server'), False)
        self.default_session = _text(general.get('default_session'))
        self.device_id = _text(general.get('device_id'))
        self.sessions = []
        for id, entry in (data.get('Sessions') or {}).items():
            entry = entry or {}
            # the room is the id: one session per room
            self.sessions.append(dict(id=id, name=_text(entry.get('name')) or id, room=id, account=_text(entry.get('account')),
                                      cameras=_names(entry.get('cameras')), audio_input=_text(entry.get('audio_input')),
                                      audio_output=_text(entry.get('audio_output')),
                                      muted=_bool(entry['muted'], False) if entry.get('muted') not in (None, '') else None))
        self.cameras = []       # RTSP cameras, then local ones: dicts with url, or device, or test (a pattern)
        rtsp = data.get('RTSPCameras') or {}
        defaults = rtsp.get('default') or {}
        self.camera_user, self.camera_password = _text(defaults.get('user')), _text(defaults.get('password'))
        for id, entry in rtsp.items():
            if id == 'default':
                continue        # the credentials of the RTSP cameras without their own
            entry = entry or {}
            camera = dict(id=id, kind='rtsp', name=_text(entry.get('name')) or id, enabled=_bool(entry.get('enabled'), True))
            for key in ('url', 'user', 'password', 'transcode', 'resolution', 'fps'):
                camera[key] = _text(entry.get(key))
            if camera['url'] and not camera['user'] and '@' not in camera['url'].partition('://')[2].partition('/')[0]:
                camera['user'], camera['password'] = self.camera_user, self.camera_password
            self.cameras.append(camera)
        for id, entry in (data.get('LocalCameras') or {}).items():
            entry = entry or {}
            camera = dict(id=id, kind='local', name=_text(entry.get('name')) or id, enabled=_bool(entry.get('enabled'), True),
                          device=_text(entry.get('device')), test=_text(entry.get('pattern')),
                          resolution=_text(entry.get('resolution')), fps=_text(entry.get('fps')))
            self.cameras.append(camera)

    def find_session(self, room_or_name):
        for session in self.sessions:
            if room_or_name in (session['id'], session['room']) or room_or_name.lower() == session['name'].lower():
                return session
        return None

    def session_for_room(self, room):
        return next((session for session in self.sessions if session['room'] and session['room'].lower() == room.lower()), None)

    def find_camera(self, name_or_id):
        """By name (case insensitive), id, or MAC address in any form (a camera's id when known)."""
        from sylkrtc.onvif import normalize_mac
        mac = normalize_mac(name_or_id)
        for camera in self.cameras:
            if name_or_id == camera['id'] or name_or_id.lower() == camera['name'].lower() or (mac and normalize_mac(camera['id']) == mac):
                return camera
        return None
