# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""SylkServer discovery: the infrastructure settings a domain publishes.

A domain served by SylkServer has a DNS TXT record _sylkserver.<domain> holding one
URL, the domain's configuration: a JSON document (wsServer, iceServers,
defaultConferenceDomain, defaultGuestDomain, conference, pstn, ...).

The TXT record is asked of the system resolver, with Google's DNS over HTTPS
(dns.google) when that fails. What was downloaded can be kept in a folder per
domain, <folder>/<domain>/configuration.json, so a later run knows it before the
network does and keeps it when a lookup or download fails.

SylkServerConfigurationDidChange is posted (sender the Discovery, data domain,
configuration, configuration_url) when a domain's configuration is new or changed.
"""

import json
import logging
import os
import threading

from datetime import datetime, timezone

from application.notification import NotificationCenter, NotificationData
from twisted.internet.threads import deferToThread

from sylkrtc.util import call_in_reactor


__all__ = ['Discovery', 'lookup_configuration_url', 'download_configuration', 'ws_server_http_url', 'DiscoveryError']


log = logging.getLogger('sylkrtc.discovery')

CACHE_FILE = 'configuration.json'
DNS_TIMEOUT = 6         # seconds
DOWNLOAD_TIMEOUT = 8    # seconds


class DiscoveryError(Exception):
    pass


def _valid_domain(domain):
    return bool(domain) and '/' not in domain and '\\' not in domain and not domain.startswith('.')


def _txt_by_resolver(name):
    import dns.resolver
    resolver = dns.resolver.Resolver()
    resolver.timeout = 3
    resolver.lifetime = DNS_TIMEOUT
    answers = resolver.resolve(name, 'TXT')
    return [b''.join(record.strings).decode('utf-8', 'replace') for record in answers]


def _txt_by_https(name):
    import requests
    response = requests.get('https://dns.google/resolve', params={'name': name, 'type': 'TXT'}, timeout=DNS_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get('Status') == 3:     # NXDOMAIN
        return []
    return [str(answer.get('data', '')).strip('"') for answer in data.get('Answer') or () if answer.get('type') == 16]


def lookup_configuration_url(domain):
    """The URL of the domain's configuration (blocking); None when the domain publishes none;
    DiscoveryError when neither DNS nor dns.google answered."""
    name = f'_sylkserver.{domain}'
    try:
        records, how = _txt_by_resolver(name), 'DNS'
    except Exception as e:
        if type(e).__name__ in ('NXDOMAIN', 'NoAnswer'):
            records, how = [], 'DNS'
        else:
            log.info(f'DNS lookup of the TXT record {name} failed ({str(e) or type(e).__name__}), asking dns.google')
            try:
                records, how = _txt_by_https(name), 'dns.google'
            except Exception as e:
                raise DiscoveryError(f'TXT record {name} not found: DNS and dns.google failed ({e})')
    urls = [record.strip() for record in records if record.strip()]
    if not urls:
        log.info(f'{domain} publishes no SylkServer configuration (no TXT record {name}, by {how})')
        return None
    if len(urls) > 1:
        raise DiscoveryError(f'{name} has {len(urls)} TXT records, expected one: {", ".join(urls)}')
    url = urls[0]
    if not url.startswith(('https://', 'http://')):
        raise DiscoveryError(f'the TXT record {name} is not a URL: {url}')
    log.info(f'{domain}: TXT record {name} -> {url} (by {how})')
    return url


def download_configuration(url):
    """The configuration document at url (blocking)."""
    import requests
    try:
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        configuration = response.json()
    except Exception as e:
        raise DiscoveryError(f'cannot download {url}: {e}')
    if not isinstance(configuration, dict):
        raise DiscoveryError(f'{url} is not a JSON object')
    return configuration


def ws_server_http_url(configuration):
    """The SylkServer web root derived from wsServer (wss://host/path/ws -> https://host/path), or None."""
    server = (configuration or {}).get('wsServer')
    if not isinstance(server, str) or not server.strip():
        return None
    server = server.strip()
    if server.startswith('wss://'):
        server = 'https://' + server[len('wss://'):]
    elif server.startswith('ws://'):
        server = 'http://' + server[len('ws://'):]
    server = server.rstrip('/')
    return server[:-3] if server.endswith('/ws') else server


class Discovery(object):
    """The configurations of the domains looked up, cached in folder when one is given."""

    def __init__(self, folder=None):
        self.folder = folder
        self.configurations = {}        # domain: {'configurationUrl', 'fetched', 'configuration'}
        self.running = {}               # domain: Deferred of the lookup in progress
        self.lock = threading.Lock()
        if folder is not None:
            self._load()

    def configuration(self, domain):
        """The configuration of a domain as last downloaded (possibly in an earlier run), or None."""
        entry = self.configurations.get(str(domain or '').lower())
        return entry.get('configuration') if entry else None

    def ws_server(self, domain):
        """The domain's wsServer, or None."""
        server = (self.configuration(domain) or {}).get('wsServer')
        if not isinstance(server, str) or not server.strip().startswith(('wss://', 'ws://')):
            return None
        return server.strip()

    def discover(self, domain):
        """Look the domain up again: a Deferred that fires with its configuration (the cached one
        when the lookup fails, None when there is none)."""
        domain = str(domain or '').strip().lower()
        if not _valid_domain(domain):
            from twisted.internet.defer import succeed
            return succeed(None)
        running = self.running.get(domain)
        if running is not None:
            from twisted.internet.defer import Deferred
            waiter = Deferred()
            running.addBoth(lambda result: (waiter.callback(self.configuration(domain)), result)[1])
            return waiter
        deferred = deferToThread(self._discover, domain)

        def done(result):
            self.running.pop(domain, None)
            return self.configuration(domain)

        def failed(failure):
            log.warning(f'discovery for {domain} failed: {failure.getErrorMessage()}')
            return None

        deferred.addErrback(failed)
        deferred.addBoth(done)
        self.running[domain] = deferred
        return deferred

    # In a thread of its own

    def _discover(self, domain):
        try:
            url = lookup_configuration_url(domain)
        except DiscoveryError as e:
            log.warning(f'{e}' + ('; keeping the cached configuration' if domain in self.configurations else ''))
            return
        if url is None:
            return
        try:
            configuration = download_configuration(url)
        except DiscoveryError as e:
            log.warning(f'configuration of {domain}: {e}' + ('; keeping the cached one' if domain in self.configurations else ''))
            return
        if not configuration.get('wsServer'):
            log.warning(f'the configuration of {domain} from {url} has no wsServer')
        entry = {'configurationUrl': url,
                 'fetched': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 'configuration': configuration}
        with self.lock:
            previous = (self.configurations.get(domain) or {}).get('configuration')
            self.configurations[domain] = entry
        self._log_changes(domain, url, previous, configuration)
        self._save(domain, entry)
        if previous != configuration:
            call_in_reactor(NotificationCenter().post_notification, 'SylkServerConfigurationDidChange', sender=self,
                            data=NotificationData(domain=domain, configuration=configuration, configuration_url=url))

    @staticmethod
    def _log_changes(domain, url, previous, configuration):
        if previous is None:
            log.info(f'configuration of {domain} (from {url}): {len(configuration)} keys')
            return
        added = sorted(set(configuration) - set(previous))
        removed = sorted(set(previous) - set(configuration))
        changed = sorted(key for key in set(configuration) & set(previous) if configuration[key] != previous[key])
        if not (added or removed or changed):
            log.info(f'configuration of {domain} unchanged ({len(configuration)} keys)')
            return
        parts = [f"{what} {', '.join(keys)}" for what, keys in (('added', added), ('removed', removed), ('changed', changed)) if keys]
        log.info(f"configuration of {domain} changed: {'; '.join(parts)}")

    # The cache

    def _path(self, domain):
        return os.path.join(self.folder, domain, CACHE_FILE)

    def _load(self):
        try:
            domains = sorted(name for name in os.listdir(self.folder) if os.path.isdir(os.path.join(self.folder, name)))
        except FileNotFoundError:
            return
        for domain in domains:
            path = self._path(domain)
            try:
                with open(path, encoding='utf-8') as cache:
                    entry = json.load(cache)
                if not isinstance(entry, dict) or not isinstance(entry.get('configuration'), dict):
                    raise ValueError('no configuration in it')
            except FileNotFoundError:
                continue
            except Exception as e:
                log.warning(f'cannot read the cached configuration of {domain} ({path}): {e}')
                continue
            self.configurations[domain] = entry
            log.info(f"cached configuration of {domain} (from {entry.get('configurationUrl')}, fetched {entry.get('fetched')})")

    def _save(self, domain, entry):
        if self.folder is None or not _valid_domain(domain):
            return
        path = self._path(domain)
        temporary = path + '.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self.lock:
                text = json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False)
            with open(temporary, 'w', encoding='utf-8') as cache:
                cache.write(text)
            os.replace(temporary, path)
        except Exception as e:
            log.warning(f'cannot write the cached configuration of {domain} ({path}): {e}')
