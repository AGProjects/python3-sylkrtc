# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""1 to 1 calls through the SylkServer webrtcgateway (its session API), independent of the media.

The gateway turns them into SIP calls of the account: session-create with our offer
for an outgoing call, its answer in session-event state accepted (or early-media);
for an incoming one, account-register, then account-event incoming-session with the
caller's offer, answered with session-answer. ICE candidates go in session-trickle,
the call ends with session-terminate.

The media is done by a backend given to the call (media_factory(call)), for example
sylkrtc.gst.CallMedia. It is told:

    start(offer=None)               make our offer (call.local_description(sdp, 'offer')), or answer the offer
    set_answer(sdp)
    add_remote_candidate(mline, candidate)
    set_audio_muted(muted)
    stop()

and tells the call, from any thread: local_description(sdp, kind), candidate(mline, mid,
candidate), end_of_candidates(), ice_failed(), media_error(source, text, fatal),
remote_video(width, height), video_closed().

Notifications:
  SylkRTCCallDidChangeState               state, reason, code      (sender the Call)
  SylkRTCCallWillTerminate                (the media is about to stop: last statistics)
  SylkRTCCallGotMessage                   sender, content, content_type
  SylkRTCCallGotRemoteVideo               width, height
  SylkRTCCallVideoWindowDidClose          (the media's own video window was closed by the user)
  SylkRTCAccountRegistrationDidChange     state, reason             (sender the Account)
  SylkRTCAccountGotIncomingCall           call
  SylkRTCAccountMissedCall                originator
  SylkRTCAccountGotAddressBook            addressbook
  SylkRTCAccountAddressBookDidChange      type, action, data   (ours, or another device's)

Everything runs in the reactor thread.
"""

import logging
import uuid

from datetime import datetime, timezone

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implementer

from sylkrtc.connection import Connections
from sylkrtc.sdp import for_sip, media_summary
from sylkrtc.util import Timer, call_in_reactor


__all__ = ['Account', 'Call']


log = logging.getLogger('sylkrtc.call')
sdp_log = logging.getLogger('sylkrtc.sdp')     # the SDP exchanged, at INFO (sylk-* --sdp)

ESTABLISH_TIMEOUT = 30      # s from accepted to established


def _identity_text(identity):
    """A SIPIdentity of the gateway (dict(uri, display_name)) as 'Name <uri>'."""
    if not isinstance(identity, dict):
        return str(identity or '')
    uri, name = identity.get('uri') or '', identity.get('display_name') or ''
    return f'{name} <{uri}>' if name and name != uri else uri


@implementer(IObserver)
class Account(object):
    """Our account at a gateway: added (enough to call), and registered to take calls (register())."""

    def __init__(self, identity, ws_server, user_agent=None, verify_tls=False, device_id=None, device_name=None):
        self.identity = identity
        self.ws_server = ws_server
        self.user_agent = user_agent
        self.verify_tls = verify_tls
        self.device_id = device_id          # this device to the server (Connection); None: the tools'
        self.device_name = device_name
        self.connection = None
        self.added = False
        self.registration = None        # None, registering, registered, failed
        self.calls = {}                 # session id -> Call (all of them on this connection)
        self._waiting = []              # callbacks(error) until added
        self._register = False
        self._addressbook_waiters = []  # callbacks(addressbook, error) of account-fetch-addressbook
        self._update_waiters = {}       # (type, action, id) -> [callbacks(data, error)] of account-update-addressbook

    def __repr__(self):
        return f'<Account {self.identity.address}>'

    @property
    def address(self):
        return self.identity.address

    def add(self, callback=None):
        """Open the connection and add the account to it; callback(error) when done."""
        if self.added and self.connection is not None and self.connection.state != 'closed':
            if callback is not None:
                callback(None)
            return
        if callback is not None:
            self._waiting.append(callback)
        if self.connection is not None and self.connection.state != 'closed':
            return
        self.connection = Connections().get(self.ws_server, user_agent=self.user_agent, verify_tls=self.verify_tls,
                                            device_id=self.device_id, device_name=self.device_name)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.connection, name='SylkRTCConnectionGotEvent')
        notification_center.add_observer(self, sender=self.connection, name='SylkRTCConnectionDidClose')
        self.connection.add_identity(self.identity.address, self.identity.password, self.identity.display_name, self._added)

    def _added(self, error):
        self.added = error is None
        waiting, self._waiting = self._waiting, []
        for callback in waiting:
            callback(error)
        if error is None and self._register:
            self._send_register()

    def register(self):
        """Take incoming calls (account-register); see registration and SylkRTCAccountRegistrationDidChange."""
        self._register = True
        if self.added:
            self._send_register()
        else:
            self.add()

    def _send_register(self):
        self._set_registration('registering')
        self.connection.request(dict(sylkrtc='account-register', account=self.identity.address),
                                lambda error: error is not None and self._set_registration('failed', error))

    def unregister(self):
        self._register = False
        if self.connection is not None and self.connection.state == 'ready' and self.registration in ('registering', 'registered'):
            self.connection.request(dict(sylkrtc='account-unregister', account=self.identity.address))
        self.registration = None

    def close(self):
        self.unregister()
        if self.connection is not None:
            notification_center = NotificationCenter()
            notification_center.discard_observer(self, sender=self.connection, name='SylkRTCConnectionGotEvent')
            notification_center.discard_observer(self, sender=self.connection, name='SylkRTCConnectionDidClose')
            connection, self.connection = self.connection, None
            Connections().release(connection)      # a close handshake after the requests already sent
        self.added = False

    def _set_registration(self, state, reason=None):
        if state == self.registration and reason is None:
            return
        self.registration = state
        NotificationCenter().post_notification('SylkRTCAccountRegistrationDidChange', sender=self, data=NotificationData(state=state, reason=reason))

    # The addressbook (XCAP, through the gateway; the account must be registered)

    def fetch_addressbook(self, callback):
        """account-fetch-addressbook: callback(addressbook, None) with dict(contacts, groups, policies), or
        callback(None, error)."""
        self._addressbook_waiters.append(callback)

        def answered(error):
            if error is not None:
                self._addressbook_result(None, error)
        self.connection.request(dict(sylkrtc='account-fetch-addressbook', account=self.identity.address), answered)

    def _addressbook_result(self, addressbook, error):
        waiters, self._addressbook_waiters = self._addressbook_waiters, []
        for callback in waiters:
            callback(addressbook, error)

    def update_addressbook(self, action, kind, data, callback):
        """account-update-addressbook: action add, update or delete of a contact, group or policy (data, a dict
        with its id); callback(data as stored, None) or callback(None, error)."""
        key = (kind, action, data.get('id'))
        self._update_waiters.setdefault(key, []).append(callback)

        def answered(error):
            if error is not None:
                self._update_result(key, None, error)
        self.connection.request(dict(sylkrtc='account-update-addressbook', account=self.identity.address, action=action, type=kind, data=data), answered)

    def _update_result(self, key, data, error):
        for callback in self._update_waiters.pop(key, []):
            callback(data, error)

    def call(self, uri, media_factory, ice_servers=None, audio=True):
        """Call someone: a Call, started."""
        call = Call(self, uri.replace('sip:', '', 1), media_factory, ice_servers=ice_servers, audio=audio)
        call.start()
        return call

    # Events

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SylkRTCConnectionDidClose(self, notification):
        self.added = False
        for call in list(self.calls.values()):
            call._terminate(f'connection to the server lost: {notification.data.reason}')
        if self.registration is not None:
            self._set_registration('failed', f'connection to the server lost: {notification.data.reason}')
        NotificationCenter().discard_observer(self, sender=notification.sender, name='SylkRTCConnectionGotEvent')
        NotificationCenter().discard_observer(self, sender=notification.sender, name='SylkRTCConnectionDidClose')
        self.connection = None

    def _NH_SylkRTCConnectionGotEvent(self, notification):
        message = notification.data.message
        kind = message.get('sylkrtc')
        if kind == 'session-event':
            call = self.calls.get(message.get('session'))
            if call is not None:
                call._event(message)
        elif kind == 'account-event' and message.get('account') == self.identity.address:
            event = message.get('event')
            if event == 'registration-state':
                self._set_registration(message.get('state'), message.get('reason'))
            elif event == 'incoming-session':
                call = Call(self, (message.get('originator') or {}).get('uri') or '', None, id=message.get('session'),
                            offer=message.get('sdp'), originator=message.get('originator'), call_id=message.get('call_id'))
                self.calls[call.id] = call
                log.info(f'incoming call from {_identity_text(call.originator)} ({media_summary(call.offer or "")})')
                sdp_log.info(f'{call.peer}: offer\n{call.offer}')
                NotificationCenter().post_notification('SylkRTCAccountGotIncomingCall', sender=self, data=NotificationData(call=call))
            elif event == 'addressbook-fetched':
                addressbook = message.get('addressbook') or {}
                self._addressbook_result(addressbook, None)
                NotificationCenter().post_notification('SylkRTCAccountGotAddressBook', sender=self, data=NotificationData(addressbook=addressbook))
            elif event == 'addressbook-updated':
                kind, action = message.get('type'), message.get('action')
                data = message.get(kind) or {}
                self._update_result((kind, action, data.get('id')), data, None)
                NotificationCenter().post_notification('SylkRTCAccountAddressBookDidChange', sender=self, data=NotificationData(type=kind, action=action, data=data))
            elif event == 'addressbook-update-failed':
                kind, action = message.get('type'), message.get('action')
                error = (message.get('error') or 'failed') + (' (temporary, try again later)' if message.get('retryable') else '')
                self._update_result((kind, action, message.get('id')), None, error)
            elif event == 'missed-session':
                NotificationCenter().post_notification('SylkRTCAccountMissedCall', sender=self, data=NotificationData(originator=_identity_text(message.get('originator'))))


class Call(object):
    """A 1 to 1 call of an account.

    States: idle, connecting (our offer is being made and sent), incoming (not answered yet), answering,
    progress, ringing, early-media, accepted, established, terminated.
    """

    def __init__(self, account, uri, media_factory, ice_servers=None, audio=True, id=None, offer=None, originator=None, call_id=None):
        self.account = account
        self.uri = uri
        self.media_factory = media_factory
        self.ice_servers = ice_servers or []
        self.id = id or str(uuid.uuid4())
        self.direction = 'incoming' if offer is not None else 'outgoing'
        self.offer = offer
        self.originator = originator
        self.call_id = call_id
        self.state = 'incoming' if offer is not None else 'idle'
        self.reason = ''
        self.code = None
        self.audio_muted = not audio
        self.media = None
        self.remote_sdp = None
        self._created = False           # the session exists at the gateway (session-create sent, or incoming)
        self._description_sent = False  # our offer or answer is out: candidates can go
        self._pending_candidates = []
        self._establish_timer = Timer(ESTABLISH_TIMEOUT, lambda: self.hangup('no media connection'))

    def __repr__(self):
        return f'<Call {self.direction} {self.uri} {self.state}>'

    @property
    def peer(self):
        return _identity_text(self.originator) if self.originator else self.uri

    def log(self, text, level='info'):
        getattr(log, level)(f'{self.peer}: {text}')

    @property
    def connection(self):
        return self.account.connection

    # Placing and answering

    def start(self):
        """An outgoing call: the account added, our offer made, then session-create."""
        if self.state != 'idle':
            return
        self._set_state('connecting')
        self.account.calls[self.id] = self
        self.log(f'calling as {self.account.address}')
        self.account.add(self._account_added)

    def _account_added(self, error):
        if self.state != 'connecting':
            return
        if error is not None:
            self._terminate(f'the server refused the account: {error}')
            return
        self._start_media()

    def answer(self, media_factory, ice_servers=None, audio=True):
        """Answer an incoming call: our answer to its offer goes in session-answer."""
        if self.state != 'incoming':
            return
        self.media_factory = media_factory
        self.ice_servers = ice_servers or self.ice_servers
        self.audio_muted = not audio
        self._created = True
        self._set_state('answering')
        self._start_media(self.offer)

    def _start_media(self, offer=None):
        self.media = self.media_factory(self)
        try:
            self.media.start(offer) if offer is not None else self.media.start()
        except Exception as e:
            self.log(f'cannot start the media: {e}', 'error')
            self.hangup(f'cannot start the media: {e}')

    def reject(self):
        if self.state == 'incoming':
            self.hangup('rejected')

    def hangup(self, reason='hung up'):
        """End the call (decline it when incoming and not answered)."""
        if self.state in ('idle', 'terminated'):
            return
        self.log('hanging up' if self.state != 'incoming' else 'rejecting')
        if self.connection is not None and self.connection.state == 'ready' and (self._created or self.direction == 'incoming'):
            self.connection.request(dict(sylkrtc='session-terminate', session=self.id))
        self._terminate(reason)

    def _terminate(self, reason, code=None):
        if self.state == 'terminated':
            return
        self._establish_timer.stop()
        self.reason = reason
        self.code = code
        if self.media is not None:
            NotificationCenter().post_notification('SylkRTCCallWillTerminate', sender=self)     # the media is still there
            try:
                self.media.stop()
            except Exception as e:
                self.log(f'stopping the media: {e}', 'warning')
        self.account.calls.pop(self.id, None)
        self.log(f'ended: {reason}')
        self._set_state('terminated', reason, code)

    def _set_state(self, state, reason='', code=None):
        self.state = state
        NotificationCenter().post_notification('SylkRTCCallDidChangeState', sender=self, data=NotificationData(state=state, reason=reason, code=code))

    # Controls

    def set_audio_muted(self, muted):
        self.audio_muted = bool(muted)
        if self.media is not None:
            self.media.set_audio_muted(self.audio_muted)
        self.log('microphone muted' if self.audio_muted else 'microphone unmuted')

    def send_dtmf(self, digits, duration=160):
        """DTMF as SIP INFO, through the gateway."""
        if self.state != 'established' or self.connection is None:
            return False
        for digit in digits:
            if digit in '0123456789*#ABCDabcd':
                self.connection.request(dict(sylkrtc='session-dtmf-info', session=self.id, digit=digit.upper(), duration=duration))
        return True

    def send_message(self, content, content_type='text/plain'):
        """A message to the far end, in the call (SIP MESSAGE through the gateway)."""
        if self.state != 'established' or self.connection is None:
            raise RuntimeError('not in a call')
        self.connection.request(dict(sylkrtc='session-message', session=self.id, message_id=uuid.uuid4().hex, content=content,
                                     content_type=content_type, timestamp=datetime.now(timezone.utc).isoformat()))

    # From the media backend (any thread)

    def local_description(self, sdp, kind):
        call_in_reactor(self._local_description, sdp, kind)

    def candidate(self, mline, mid, candidate):
        if candidate.startswith('a='):
            candidate = candidate[2:]
        call_in_reactor(self._trickle, [dict(candidate=candidate, sdpMLineIndex=mline, sdpMid=mid if mid is not None else str(mline))])

    def end_of_candidates(self):
        call_in_reactor(self._trickle, [])

    def ice_failed(self):
        call_in_reactor(self.hangup, 'the media connection failed (ICE)')

    def media_error(self, source, text, fatal):
        call_in_reactor(self._media_error, source, text, fatal)

    def remote_video(self, width, height):
        call_in_reactor(NotificationCenter().post_notification, 'SylkRTCCallGotRemoteVideo', sender=self, data=NotificationData(width=width, height=height))

    def video_closed(self):
        call_in_reactor(NotificationCenter().post_notification, 'SylkRTCCallVideoWindowDidClose', sender=self)

    def _local_description(self, sdp, kind):
        if self.state not in ('connecting', 'answering') or self.connection is None:
            return
        sdp = for_sip(sdp)
        sdp_log.info(f'{self.peer}: our {kind}\n{sdp}')
        if kind == 'offer':
            self.log(f'sending the offer ({media_summary(sdp)})')
            self.connection.request(dict(sylkrtc='session-create', account=self.account.address, session=self.id, uri=self.uri, sdp=sdp), self._created_answered)
            self._created = True
        else:
            self.log(f'answering ({media_summary(sdp)})')
            self.connection.request(dict(sylkrtc='session-answer', session=self.id, sdp=sdp),
                                    lambda error: error is not None and self._terminate(f'cannot answer: {error}'))
        self._description_sent = True
        pending, self._pending_candidates = self._pending_candidates, []
        for candidates in pending:
            self._trickle(candidates)

    def _created_answered(self, error):
        if error is not None:
            self._terminate(f'cannot call: {error}')

    def _trickle(self, candidates):
        if self.state == 'terminated' or self.connection is None:
            return
        if not self._description_sent:
            self._pending_candidates.append(candidates)       # the session must have our offer or answer first
            return
        self.connection.request(dict(sylkrtc='session-trickle', session=self.id, candidates=candidates))

    def _media_error(self, source, text, fatal):
        if self.state == 'terminated':
            return
        self.log(f'media error in {source}: {text}', 'error' if fatal else 'warning')
        if fatal:
            self.hangup(f'media error: {text}')

    # Signalling events

    def _event(self, message):
        event = message.get('event')
        if event == 'state':
            self._state_event(message)
        elif event == 'message':
            sender = _identity_text(message.get('sender'))
            NotificationCenter().post_notification('SylkRTCCallGotMessage', sender=self, data=NotificationData(
                sender=sender, content=message.get('content') or '', content_type=message.get('content_type') or 'text/plain'))
        elif event == 'update' and message.get('state') == 'received':
            self.log('the far end changed the call (re-INVITE): not supported, the media stays as it is', 'warning')

    def _state_event(self, message):
        state = message.get('state')
        if self.state == 'terminated':
            return
        if message.get('call_id'):
            self.call_id = message['call_id']
        if state == 'progress':
            self._set_state('progress')
        elif state == 'proceeding':
            self.log(f'proceeding ({message.get("code")})')
            self._set_state('progress', code=message.get('code'))
        elif state == 'ringing':
            self.log('ringing')
            self._set_state('ringing')
        elif state == 'early-media':
            self.log('early media')
            if message.get('sdp') and self.remote_sdp is None:
                self._remote_answer(message['sdp'])
            self._set_state('early-media')
        elif state == 'accepted':
            if message.get('sdp') and self.remote_sdp is None:
                self._remote_answer(message['sdp'])
            self.log('accepted')
            self._set_state('accepted')
            self._establish_timer.start()
        elif state == 'established':
            self._establish_timer.stop()
            self.log('established')
            self._set_state('established')
        elif state == 'terminated':
            reason = message.get('reason') or 'ended'
            self._terminate(reason)

    def _remote_answer(self, sdp):
        self.remote_sdp = sdp
        self.log(f'answer: {media_summary(sdp)}')
        sdp_log.info(f'{self.peer}: answer\n{sdp}')
        try:
            self.media.set_answer(sdp)
        except Exception as e:
            self.log(f'cannot use the answer: {e}', 'error')
            self.hangup(f'media error: {e}')
