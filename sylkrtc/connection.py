# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""A client of the sylkRTC protocol: the WebSocket API of SylkServer's webrtcgateway (sylkRTC-2).

Requests are JSON objects with a sylkrtc name and a transaction id; the server
answers each with an ack or an error carrying the same transaction. Everything
else it sends is an event: ready-event once the connection is usable, then
account-event, session-event and videoroom-event.

One connection per wsServer URL, shared by the identities on it (Connections).
An identity is added to a connection (account-add) before it is used. Nothing here
registers it: a registration from the gateway would fork incoming calls and
messages to the gateway, which a SIP client registered elsewhere does not want.

Every account-add says which device the client is (deviceId, deviceName): the
server lists the devices that use the account. A client gives its own with
Connection(device_id=, device_name=) (Blink: its SIP instance id and "Blink Qt on
<host> (Linux)"); without them the package's tools identity is used
(sylkrtc.device: python-sylkrtc on <host>, its id kept in ~/.sylkclient/config,
which is only read or made then).

Notifications, sender the connection:
  SylkRTCConnectionDidBecomeReady    features
  SylkRTCConnectionDidClose          reason
  SylkRTCConnectionGotEvent          message (a dict)

Everything runs in the reactor thread.
"""

import json
import logging
import uuid

from hashlib import md5
from urllib.parse import urlsplit

from application.notification import NotificationCenter, NotificationData
from application.python.types import Singleton
from autobahn.twisted.websocket import WebSocketClientFactory, WebSocketClientProtocol, connectWS
from twisted.internet import reactor

from sylkrtc import PROTOCOL
from sylkrtc.device import USER_AGENT, device_id, device_name
from sylkrtc.util import Timer, run_in_reactor


__all__ = ['Connection', 'Connections', 'ha1', 'DEFAULT_USER_AGENT']


log = logging.getLogger('sylkrtc.connection')

PING_INTERVAL = 30      # s, a WebSocket ping keeps NATs and proxies from dropping the connection
PING_TIMEOUT = 20       # s without pong: the connection is dead
CONNECT_TIMEOUT = 15    # s until ready-event
REQUEST_TIMEOUT = 20    # s without ack or error: the request failed

DEFAULT_USER_AGENT = USER_AGENT     # the same for every tool: one device on the server (sylkrtc.device)


def ha1(username, domain, password):
    """What account-add takes as password: HA1 of the SIP credentials, as sylkrtc.js sends it."""
    return md5(f'{username}:{domain}:{password or ""}'.encode()).hexdigest()


class _Protocol(WebSocketClientProtocol):
    def onConnect(self, response):
        self.factory.connection._connected(self, response.protocol, response.headers)

    def onMessage(self, payload, is_binary):
        if not is_binary:
            self.factory.connection._received(payload.decode('utf-8', 'replace'))

    def onClose(self, was_clean, code, reason):
        self.factory.connection._disconnected(reason or (f'code {code}' if code else 'disconnected'))


class _Factory(WebSocketClientFactory):
    protocol = _Protocol

    def clientConnectionFailed(self, connector, reason):
        self.connection._fail(reason.getErrorMessage())

    def clientConnectionLost(self, connector, reason):
        self.connection._disconnected(reason.getErrorMessage())


class Connection(object):
    """A WebSocket to one webrtcgateway.

    request(data, callback) sends a request once the connection is ready (queued until
    then) and calls callback(error) when it is answered: error is None for an ack, the
    server's reason otherwise.
    """

    def __init__(self, url, user_agent=None, verify_tls=False, device_id=None, device_name=None):
        self.url = url
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.verify_tls = verify_tls
        self.device_id = device_id          # sent with account-add; None: the tools' (sylkrtc.device), made when first needed
        self.device_name = device_name
        self.state = 'idle'             # idle, connecting, ready, closed
        self.accounts = set()           # identities added
        self.features = set()           # what the gateway announced in its ready-event (sip-instance, multiple-video-feeds)
        self.server = None              # the Server header of the handshake (SylkServer/<version>)
        self.subprotocol = None
        self.connect_time = None        # s from open to ready-event
        self._pending_accounts = {}     # identity -> callbacks waiting for account-add
        self._queue = []                # requests made before ready
        self._transactions = {}         # transaction -> (request name, callback, timer)
        self._protocol = None
        self._connector = None
        self._connect_timer = Timer(CONNECT_TIMEOUT, lambda: self._fail('no answer from the server'))

    def __repr__(self):
        return f'<Connection {self.url} {self.state}>'

    # Connection

    def open(self):
        if self.state in ('connecting', 'ready'):
            return
        self.state = 'connecting'
        log.info(f'connecting to {self.url}')
        self._opened = reactor.seconds()
        factory = _Factory(self.url, protocols=[PROTOCOL], useragent=self.user_agent)
        factory.connection = self
        factory.setProtocolOptions(autoPingInterval=PING_INTERVAL, autoPingTimeout=PING_TIMEOUT, openHandshakeTimeout=CONNECT_TIMEOUT)
        context = None
        if factory.isSecure:
            from twisted.internet import ssl
            if self.verify_tls:
                context = ssl.optionsForClientTLS(urlsplit(self.url).hostname)
            else:
                context = ssl.CertificateOptions(verify=False)
        self._connector = connectWS(factory, contextFactory=context, timeout=CONNECT_TIMEOUT)
        self._connect_timer.start()

    def close(self, reason='closed'):
        if self.state in ('idle', 'closed'):
            return
        log.info(f'closing the connection to {self.url}')
        self._drop()
        self._finish(reason)

    def _drop(self):
        if self._protocol is not None:
            protocol, self._protocol = self._protocol, None
            try:
                protocol.sendClose()
            except Exception:
                pass
            reactor.callLater(2, lambda: protocol.transport is not None and protocol.transport.loseConnection())
        elif self._connector is not None:
            try:
                self._connector.disconnect()
            except Exception:
                pass
        self._connector = None

    def _fail(self, reason):
        if self.state in ('idle', 'closed'):
            return
        log.warning(f'connection to {self.url} failed: {reason}')
        self._drop()
        self._finish(reason)

    def _finish(self, reason):
        if self.state == 'closed':
            return
        self.state = 'closed'
        self._connect_timer.stop()
        self.accounts.clear()
        pending = [callback for callbacks in self._pending_accounts.values() for callback in callbacks]
        self._pending_accounts.clear()
        queued, self._queue = self._queue, []
        transactions, self._transactions = self._transactions, {}
        for name, callback, timer in transactions.values():
            timer.stop()
            if callback is not None:
                callback(reason)
        for data, callback in queued:
            if callback is not None:
                callback(reason)
        for callback in pending:
            callback(reason)
        NotificationCenter().post_notification('SylkRTCConnectionDidClose', sender=self, data=NotificationData(reason=reason))

    def _connected(self, protocol, subprotocol, headers):
        if self.state != 'connecting':
            protocol.sendClose()
            return
        self._protocol = protocol
        self.subprotocol = subprotocol
        server = (headers or {}).get('server')
        self.server = ', '.join(server) if isinstance(server, (list, tuple)) else server
        log.info(f'connected to {self.url} ({self.server or "server unknown"}, protocol {subprotocol or "none"}), waiting for the server')

    @property
    def peer_certificate(self):
        """The server's TLS certificate (an OpenSSL X509), or None."""
        transport = getattr(self._protocol, 'transport', None)
        try:
            return transport.getPeerCertificate() if transport is not None else None
        except Exception:
            return None

    def _disconnected(self, reason):
        if self.state == 'closed':
            return
        log.info(f'disconnected from {self.url}: {reason}')
        self._protocol = None
        self._finish(str(reason))

    # Messages

    def _received(self, text):
        try:
            message = json.loads(text)
        except ValueError:
            log.warning(f'unreadable message from {self.url}: {text[:200]}')
            return
        log.debug(f'received {text}')
        kind = message.get('sylkrtc')
        if kind == 'ready-event':
            self._connect_timer.stop()
            self.state = 'ready'
            self.connect_time = reactor.seconds() - self._opened
            features = message.get('features')
            self.features = {feature for feature in features if isinstance(feature, str)} if isinstance(features, list) else set()
            log.info(f'{self.url} is ready' + (f' ({", ".join(sorted(self.features))})' if self.features else ''))
            queued, self._queue = self._queue, []
            for data, callback in queued:
                self._send(data, callback)
            NotificationCenter().post_notification('SylkRTCConnectionDidBecomeReady', sender=self, data=NotificationData(features=set(self.features)))
        elif kind in ('ack', 'error'):
            name, callback, timer = self._transactions.pop(message.get('transaction'), (None, None, None))
            if timer is not None:
                timer.stop()
            if kind == 'error':
                log.warning(f'{name or "request"} failed: {message.get("error")}')
            if callback is not None:
                callback((message.get('error') or 'error') if kind == 'error' else None)
        elif kind:
            NotificationCenter().post_notification('SylkRTCConnectionGotEvent', sender=self, data=NotificationData(message=message))

    @run_in_reactor
    def request(self, data, callback=None):
        data = dict(data)
        data['transaction'] = str(uuid.uuid4())
        if self.state == 'ready':
            self._send(data, callback)
        elif self.state == 'connecting':
            self._queue.append((data, callback))
        elif callback is not None:
            callback('not connected')

    def _send(self, data, callback):
        transaction = data['transaction']
        timer = Timer(REQUEST_TIMEOUT, lambda: self._timeout(transaction))
        self._transactions[transaction] = (data.get('sylkrtc'), callback, timer)
        timer.start()
        text = json.dumps(data)
        log.debug(f'sending {text if data.get("sylkrtc") != "account-add" else data["sylkrtc"]}')
        self._protocol.sendMessage(text.encode('utf-8'), isBinary=False)

    def _timeout(self, transaction):
        name, callback, timer = self._transactions.pop(transaction, (None, None, None))
        if name is None:
            return
        log.warning(f'{name} timed out')
        if callback is not None:
            callback('timeout')

    # Identities

    def add_identity(self, account_id, password, display_name, callback):
        """account-add an address with its HA1 (ours, or a guest one at another server), without
        registering it; callback(error) when done."""
        if account_id in self.accounts:
            callback(None)
            return
        if account_id in self._pending_accounts:
            self._pending_accounts[account_id].append(callback)
            return
        self._pending_accounts[account_id] = [callback]

        def added(error):
            callbacks = self._pending_accounts.pop(account_id, [])
            if error is None:
                self.accounts.add(account_id)
                log.info(f'account {account_id} added to {self.url}')
            for function in callbacks:
                function(error)

        self.open()
        # deviceId and deviceName: the device the server records as using the account (its devices.py)
        self.request(dict(sylkrtc='account-add', account=account_id, password=password,
                          display_name=display_name or '', user_agent=self.user_agent,
                          deviceId=self.device_id or device_id(), deviceName=self.device_name or device_name()), added)


class Connections(object, metaclass=Singleton):
    """The connections, by wsServer URL. get() makes one with the arguments given (user_agent,
    verify_tls, device_id, device_name); a connection already open to that URL is returned as
    it is, with the arguments it was made with: one client per process is the intended use."""

    def __init__(self):
        self._connections = {}

    def get(self, url, **kw):
        connection = self._connections.get(url)
        if connection is None or connection.state == 'closed':
            connection = self._connections[url] = Connection(url, **kw)
        return connection

    def find(self, url):
        return self._connections.get(url)

    def release(self, connection):
        """Nothing uses the connection any more: close it."""
        if self._connections.get(connection.url) is connection:
            del self._connections[connection.url]
        connection.close('not used')
