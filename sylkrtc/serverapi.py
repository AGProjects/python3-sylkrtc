# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""SylkServer's HTTP API for an account, authorised by the account's API token (account storage,
message journal): what sylk-account and sylk-journal share.

The token is the one application/sylk-api-token hands out over SIP. The gateway does not give it
to a WebSocket client, so it is borrowed from a client configuration that keeps it (Blink:
~/.blink/config), or given on the command line. The server is the one the domain's SylkServer
discovery names (wsServer, cached in ~/.sylkclient/sylkserver).

Every request says which device sends it (X-Sylk-Device-Id, X-Sylk-Device-Name): the server keeps
the list of the devices that use the account (sylk-account devices). All the tools are one device,
python-sylkrtc on this host (sylkrtc.device).
"""

import logging
import os
import urllib.parse

import requests

from sylkrtc import sipconfig
from sylkrtc.device import USER_AGENT, device_id as _tools_device_id, device_name as _tools_device_name
from sylkrtc.discovery import Discovery, ws_server_http_url


__all__ = ['ServerAPI']

log = logging.getLogger('sylkrtc.serverapi')

TIMEOUT = (15, 60)

def api_root(account_info):
    """https://host/webrtcgateway of the account's SylkServer (discovery: cached, else looked up)."""
    discovery = Discovery(os.path.join(sipconfig.DEFAULT_DIRECTORY, 'sylkserver'))
    configuration = discovery.configuration(account_info.domain)
    if not configuration or not configuration.get('wsServer'):
        log.info(f'looking up the SylkServer of {account_info.domain}')
        discovery._discover(account_info.domain.lower())       # blocking, and cached for the next run
        configuration = discovery.configuration(account_info.domain)
    root = ws_server_http_url(configuration)
    if not root:
        raise SystemExit(f'{account_info.domain} publishes no SylkServer configuration with a wsServer')
    return root


class ServerAPI(object):
    """The account's HTTP API: request('GET', 'storage/<account>/avatars'), paths below /webrtcgateway."""

    def __init__(self, program, account_info, token, verify_tls, root=None, user_agent=None, device_id=None, device_name=None):
        self.program = program
        self.account_info = account_info
        self.account = account_info.id.lower()
        self.token = token
        self.verify_tls = verify_tls
        self.root = root or api_root(account_info)
        # who sends the requests: the client's own when given, else the tools' (sylkrtc.device)
        self.user_agent = user_agent or USER_AGENT
        self.device_id = device_id or _tools_device_id()
        self.device_name = device_name or _tools_device_name()

    @classmethod
    def connect(cls, program, account=None, config_dir=None, token=None):
        """The API of an account found in the configurations (sipconfig.locate_account), with its token."""
        try:
            configuration = sipconfig.SylkConfiguration()
            account_info, _ = sipconfig.locate_account(account, os.path.expanduser(config_dir) if config_dir else None)
        except sipconfig.ConfigError as e:
            raise SystemExit(str(e))
        if not token:
            token, source = sipconfig.api_token(account_info.id)
            if not token:
                raise SystemExit(f'no API token for {account_info.id}: no client configuration keeps one '
                                 f'({", ".join(os.path.join(d, "config") for d in sipconfig.SEARCH_DIRECTORIES)}); '
                                 f'run Blink with the account once, or give --token')
            log.debug(f'API token from {source}')
        if not configuration.tls_verify_server:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)     # verification is off by choice (tls_verify_server)
        return cls(program, account_info, token, configuration.tls_verify_server)

    @property
    def quoted_account(self):
        return urllib.parse.quote(self.account, safe='@')

    def url(self, path):
        return f'{self.root}/{path.lstrip("/")}'

    def request(self, method, path, allow=(), **kw):
        """One request; SystemExit with what went wrong unless its status is below 400 or in allow."""
        url = self.url(path)
        # who we are, on every request: the server lists the devices that use the account
        headers = {'Authorization': f'Apikey {self.token}', 'User-Agent': self.user_agent,
                   'X-Sylk-Device-Id': self.device_id,
                   'X-Sylk-Device-Name': urllib.parse.quote(self.device_name, safe=" '()")}
        headers.update(kw.pop('headers', {}))
        log.debug(f'{method} {url}')
        try:
            response = requests.request(method, url, headers=headers, timeout=kw.pop('timeout', TIMEOUT), verify=self.verify_tls, **kw)
        except requests.RequestException as e:
            raise SystemExit(f'{self.program}: {method} {url}: {e}')
        log.debug(f'{response.status_code} {response.headers.get("Content-Type")} {len(response.content)} bytes')
        if response.status_code >= 400 and response.status_code not in allow:
            try:
                error = response.json().get('error')
            except ValueError:
                error = response.text[:200]
            hint = {401: ' (the token is not valid any more: open Blink, it asks for a new one, or give --token)',
                    404: ' (the server may not have this yet)'}.get(response.status_code, '')
            raise SystemExit(f'{self.program}: {method} {url}: HTTP {response.status_code} {error}{hint}')
        return response
