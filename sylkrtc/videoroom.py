# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""Joining a SylkServer video room: the sylkRTC signalling, independent of the media.

SylkServer rooms are a Janus videoroom (an SFU) behind the webrtcgateway: every
participant publishes on one PeerConnection and subscribes to each other publisher
on one PeerConnection per publisher (a feed). The signalling: videoroom-join with
our offer, the answer in session-state accepted, the publishers in
initial-publishers and publishers-joined/left, then per publisher
videoroom-feed-attach, the server's offer in feed-attached and our
videoroom-feed-answer.

The media is done by a backend given to the session (media_factory(session)), for
example sylkrtc.gst.RoomMedia. It is told:

    start()                         build the publisher; its offer goes to session.publisher_offer(sdp)
    set_publisher_answer(sdp)
    add_feed(participant, sdp)      a feed's offer; the answer goes to session.feed_answer(participant, sdp)
    remove_feed(participant)
    set_audio_muted(muted)
    create_extra(extra)             the media of an extra video (start(), set_answer(sdp), stop())
    stop()

and tells the session, from any thread (the session hands it to the reactor):
candidate(session_id, mline, mid, candidate), end_of_candidates(session_id),
ice_failed(label), level(label, loud), media_error(source, text, fatal).

Publishers of type sip are callers behind the room's audio bridge: they have no
feed. The bridge itself (type bridge) carries their mixed audio: it is subscribed
to, but not shown.

Notifications, sender the session (or the extra video):
  VideoroomSessionDidChangeState          state, reason
  VideoroomSessionParticipantDidJoin      participant
  VideoroomSessionParticipantDidLeave     participant
  VideoroomSessionParticipantDidChange    participant
  VideoroomSessionAudioMutedDidChange     muted, by (None, or who muted us)
  VideoroomSessionSpeakersDidChange       speakers, originator
  VideoroomSessionRaisedHandsDidChange    raised_hands
  VideoroomSessionInviteStatus            uri, state, code, reason
  VideoroomSessionGotMessage              type, content, content_type, sender_uri, sender_name, timestamp
  VideoroomSessionDidDeliverMessage       message_id, delivered, code, reason
  VideoroomExtraVideoDidChangeState       state, reason   (sender the ExtraVideo)

Everything runs in the reactor thread.
"""

import logging
import os
import re
import uuid

from hashlib import md5
from time import monotonic

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implementer

from sylkrtc.connection import Connections, ha1
from sylkrtc.sdp import media_summary, offers_video, video_ssrc, chosen_video_codec
from sylkrtc.util import Timer, call_in_reactor, call_later


__all__ = ['Identity', 'Participant', 'VideoroomSession', 'ExtraVideo', 'MAX_EXTRA_VIDEOS']


log = logging.getLogger('sylkrtc.videoroom')
sdp_log = logging.getLogger('sylkrtc.sdp')     # the SDP exchanged, at INFO (sylk-* --sdp)

PUBLISHER_TIMEOUT = 20      # s from the offer to established
EXTRA_TIMEOUT = 25          # s, the same for an extra video
SPEAKING_SIP_LEVEL = 25     # the room's audio levels of SIP participants (the mixer's 0-255 scale) above which they speak
SPEAKING_HOLD = 0.6         # s speaking lasts after the last loud level
MAX_EXTRA_VIDEOS = 4        # what the server takes per participant


class Identity(object):
    """Who joins: an address, the HA1 of its credentials and a display name."""

    def __init__(self, address, password_ha1, display_name='', instance_id=None):
        self.address = address
        self.password = password_ha1
        self.display_name = display_name
        self.instance_id = instance_id

    @classmethod
    def from_credentials(cls, address, password, display_name='', auth_username=None, instance_id=None):
        username, _, domain = address.partition('@')
        return cls(address, ha1(auth_username or username, domain, password), display_name, instance_id)

    @property
    def domain(self):
        return self.address.partition('@')[2]

    def __repr__(self):
        return f'<Identity {self.address}>'


class Participant(object):
    """Someone in the room: a WebRTC publisher (with a feed we subscribe to) or a SIP caller behind the bridge."""

    def __init__(self, session, publisher):
        self.session = session
        self.id = publisher.get('id')
        self.uri = (publisher.get('uri') or '').replace('sip:', '', 1)
        self.display_name = publisher.get('display_name') or ''
        self.type = publisher.get('type') or 'webrtc'
        self.parent = publisher.get('parent') or None   # an extra video of another participant: that one's publisher id
        self.label = publisher.get('label') or None
        self.muted = bool(publisher.get('muted'))
        self.feed = None            # feed id once attached
        self.state = None           # None, attaching, connecting, established, failed
        self.video = False if self.type == 'sip' else None     # whether its feed carries video, None until its offer
        self.video_codec = None
        self.speaking = False
        self._speaking_until = 0.0
        self.is_self = False        # our own SIP leg into the room, seen behind the bridge
        self.media = None           # what the media backend keeps for it

    def __repr__(self):
        return f'<Participant {self.uri} {self.type} {self.state}>'

    @property
    def name(self):
        if self.parent:
            owner = self.session.participants.get(self.parent)
            return f'{owner.name if owner is not None else self.uri} · {self.label or "Camera"}'
        return self.display_name or self.uri

    @property
    def subscribable(self):
        return self.type != 'sip'

    @property
    def visible(self):
        return self.type != 'bridge' and not self.is_self

    def set_level(self, loud):
        now = monotonic()
        if loud:
            self._speaking_until = now + SPEAKING_HOLD
        speaking = now < self._speaking_until
        if speaking != self.speaking:
            self.speaking = speaking
            self._changed()

    def update(self, publisher):
        changed = False
        if publisher.get('muted') is not None and bool(publisher['muted']) != self.muted:
            self.muted = bool(publisher['muted'])
            changed = True
        if publisher.get('display_name') and publisher['display_name'] != self.display_name:
            self.display_name = publisher['display_name']
            changed = True
        if changed:
            self._changed()

    def _changed(self):
        NotificationCenter().post_notification('VideoroomSessionParticipantDidChange', sender=self.session, data=NotificationData(participant=self))


@implementer(IObserver)
class VideoroomSession(object):
    """One room joined by one identity.

    ws_server and ice_servers: the room's SylkServer (its configuration). guest_domain: the server's
    defaultGuestDomain, to join as a guest when it does not take our domain (a room of another server).
    audio: the microphone is on (it starts muted otherwise). video: we publish a video of our own.
    receive_video: the feeds' video is sent to us (else it is paused at the server, audio going on).
    """

    def __init__(self, identity, room, ws_server, media_factory, ice_servers=None, guest_domain=None,
                 audio=True, video=False, receive_video=True, user_agent=None, verify_tls=False, device_id=None, device_name=None):
        self.identity = identity
        self.room = room.replace('sip:', '', 1)
        self.ws_server = ws_server
        self.ice_servers = ice_servers or []
        self.guest_domain = guest_domain
        self.address = identity.address     # who we are in the room: ourselves, or a guest of the other server
        self.guest = False
        self.user_agent = user_agent
        self.verify_tls = verify_tls
        self.device_id = device_id          # this device to the server (Connection); None: the tools'
        self.device_name = device_name
        self.id = str(uuid.uuid4())
        self.state = 'idle'                 # idle, connecting, accepted, established, terminated
        self.reason = ''
        self.participants = {}              # publisher id -> Participant
        self.feeds = {}                     # feed id -> Participant
        self.speakers = []
        self.raised_hands = []
        self.extra_videos = {}              # session id -> ExtraVideo
        self.audio_muted = not audio
        self.video = video
        self.video_codec = self.video_pt = None     # what our own publisher sends, when it sends video
        self.video_paused = not receive_video
        self.connection = None
        self.media = media_factory(self)
        self._muting_everyone = False
        self._join_sent = False
        self._trickle_ready = set()         # session ids whose creating request has been sent
        self._trickle_pending = {}
        self._invitees = []
        self._establish_timer = Timer(PUBLISHER_TIMEOUT, lambda: self._terminate('no media connection'))

    def __repr__(self):
        return f'<VideoroomSession {self.room} {self.state}>'

    def log(self, text, level='info'):
        getattr(log, level)(f'{self.room}: {text}')

    @property
    def features(self):
        return set(self.connection.features) if self.connection is not None else set()

    # Joining and leaving

    def join(self):
        if self.state != 'idle':
            return
        self._set_state('connecting')
        self.log(f'joining as {self.identity.address} through {self.ws_server}')
        self.connection = Connections().get(self.ws_server, user_agent=self.user_agent, verify_tls=self.verify_tls,
                                            device_id=self.device_id, device_name=self.device_name)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.connection, name='SylkRTCConnectionGotEvent')
        notification_center.add_observer(self, sender=self.connection, name='SylkRTCConnectionDidClose')
        self.connection.add_identity(self.identity.address, self.identity.password, self.identity.display_name, self._account_added)

    def _join_as_guest(self):
        name = re.sub(r'[^a-z0-9]+', '', (self.identity.display_name or self.identity.address.partition('@')[0]).lower()) or 'guest'
        self.guest = True
        self.address = '%s%s@%s' % (name, os.urandom(2).hex(), self.guest_domain)
        username, _, domain = self.address.partition('@')
        password = md5(f'{username}:{domain}:{os.urandom(8).hex()}'.encode()).hexdigest()     # guests do not register
        self.log(f'the server does not take {self.identity.domain}: joining as the guest {self.address}')
        self.connection.add_identity(self.address, password, self.identity.display_name or self.identity.address, self._account_added)

    def _account_added(self, error):
        if self.state != 'connecting':
            return
        if error is not None and self.guest_domain and not self.guest and 'not allowed' in str(error).lower():
            self._join_as_guest()
            return
        if error is not None:
            self._terminate(f'the server refused the account: {error}')
            return
        try:
            self.media.start()
        except Exception as e:
            self.log(f'cannot start the media: {e}', 'error')
            self._terminate(f'cannot start the media: {e}')
            return
        self._establish_timer.start()

    def leave(self):
        if self.state in ('idle', 'terminated'):
            return
        self.log('leaving')
        for extra in list(self.extra_videos.values()):
            extra.stop('left')
        if self.connection is not None and self.connection.state == 'ready' and self._join_sent:
            self.connection.request(dict(sylkrtc='videoroom-leave', session=self.id))
        self._terminate('left')

    def _terminate(self, reason):
        if self.state == 'terminated':
            return
        for extra in list(self.extra_videos.values()):
            extra._end('left')
        self.reason = reason
        self._establish_timer.stop()
        try:
            self.media.stop()
        except Exception as e:
            self.log(f'stopping the media: {e}', 'warning')
        if self.connection is not None:
            notification_center = NotificationCenter()
            notification_center.discard_observer(self, sender=self.connection, name='SylkRTCConnectionGotEvent')
            notification_center.discard_observer(self, sender=self.connection, name='SylkRTCConnectionDidClose')
            connection, self.connection = self.connection, None
            call_later(1, Connections().release, connection)
        self.log(f'ended: {reason}')
        self._set_state('terminated', reason)

    def _set_state(self, state, reason=''):
        self.state = state
        NotificationCenter().post_notification('VideoroomSessionDidChangeState', sender=self, data=NotificationData(state=state, reason=reason))

    # From the media backend (any thread)

    def publisher_offer(self, sdp):
        call_in_reactor(self._send_join, sdp)

    def feed_answer(self, participant, sdp):
        call_in_reactor(self._send_feed_answer, participant, sdp)

    def candidate(self, session_id, mline, mid, candidate):
        if candidate.startswith('a='):
            candidate = candidate[2:]
        call_in_reactor(self._trickle, session_id, [dict(candidate=candidate, sdpMLineIndex=mline, sdpMid=mid if mid is not None else str(mline))])

    def end_of_candidates(self, session_id):
        call_in_reactor(self._trickle, session_id, [])

    def ice_failed(self, label):
        call_in_reactor(self._ice_failed, label)

    def level(self, label, loud):
        call_in_reactor(self._level, label, loud)

    def media_error(self, source, text, fatal):
        call_in_reactor(self._media_error, source, text, fatal)

    # The publisher

    def _send_join(self, sdp):
        if self.state != 'connecting':
            return
        self.log(f'sending the offer ({media_summary(sdp)})')
        sdp_log.info(f'{self.room}: offer\n{sdp}')
        if self.video and video_ssrc(sdp) is None:
            self.log('the offer has no SSRC for our video: the server may drop it', 'warning')
        request = dict(sylkrtc='videoroom-join', account=self.address, session=self.id, uri=self.room, sdp=sdp, audio=True, video=bool(self.video))
        if not self.guest and self.identity.instance_id and 'sip-instance' in self.features:
            request['sip_instance'] = str(self.identity.instance_id)
        self.connection.request(request, self._join_answered)
        self._join_sent = True
        self._flush_trickle(self.id)

    def _join_answered(self, error):
        if error is not None:
            self._terminate(f'cannot join: {error}')

    def _on_answer(self, sdp):
        self.log(f'answer: {media_summary(sdp)}')
        sdp_log.info(f'{self.room}: answer\n{sdp}')
        codec = chosen_video_codec(sdp)
        if codec is not None:
            self.video_codec, self.video_pt = codec
        try:
            self.media.set_publisher_answer(sdp)
        except Exception as e:
            self.log(f'cannot use the answer: {e}', 'error')
            self._terminate(f'media error: {e}')

    # Controls

    def set_audio_muted(self, muted, by=None):
        self.audio_muted = bool(muted)
        self.media.set_audio_muted(self.audio_muted)
        self.log(('microphone muted' if self.audio_muted else 'microphone unmuted') + (f' by {by}' if by else ''))
        NotificationCenter().post_notification('VideoroomSessionAudioMutedDidChange', sender=self, data=NotificationData(muted=self.audio_muted, by=by))

    def set_speakers(self, ids):
        """Make these publishers (ours is self.id; none, one or two) the room's speakers, shown large to everyone."""
        if self.connection is None or self.state != 'established':
            return
        ids = [str(id) for id in ids][:2]
        self.connection.request(dict(sylkrtc='videoroom-configure', session=self.id, active_participants=ids))

    def toggle_hand(self, session_id=None):
        """Raise or lower our hand; with session_id, lower someone else's."""
        if self.connection is None or self.state != 'established':
            return
        request = dict(sylkrtc='videoroom-toggle-hand', session=self.id)
        if session_id is not None:
            request['session_id'] = session_id
        self.connection.request(request)

    def mute_everyone(self):
        if self.connection is None or self.state != 'established':
            return
        self._muting_everyone = True
        call_later(3, setattr, self, '_muting_everyone', False)
        self.connection.request(dict(sylkrtc='videoroom-mute-audio-participants', session=self.id))

    def mute_participant(self, participant, muted=True):
        if self.connection is None or self.state != 'established':
            return
        self.connection.request(dict(sylkrtc='videoroom-mute-participant', session=self.id, participant_id=participant.id, muted=bool(muted)))

    def remove_participants(self, uris):
        uris = [uri if uri.startswith('sip:') else 'sip:' + uri for uri in uris]
        if uris and self.connection is not None and self.state == 'established':
            self.connection.request(dict(sylkrtc='videoroom-remove', session=self.id, participants=uris))

    def invite(self, uris):
        uris = [uri.replace('sip:', '', 1) for uri in uris]
        if self.state != 'established':
            self._invitees.extend(uris)
            return
        self.log(f'inviting {", ".join(uris)}')
        self.connection.request(dict(sylkrtc='videoroom-invite', session=self.id, participants=uris))

    def speaker_name(self, id):
        if id == self.id:
            return 'us'
        extra = self.extra_videos.get(id)
        if extra is not None:
            return f'our {extra.label}'
        participant = self.participants.get(id)
        return participant.name if participant is not None else id

    def set_video_paused(self, paused):
        """Stop (or resume) the video of every feed at the server, audio going on."""
        paused = bool(paused)
        if paused == self.video_paused:
            return
        self.video_paused = paused
        for participant in list(self.feeds.values()):
            self._update_feed_video(participant)

    def _update_feed_video(self, participant):
        if self.connection is None or participant.feed not in self.feeds or participant.state not in ('connecting', 'established'):
            return
        self.connection.request(dict(sylkrtc='videoroom-session-update', session=participant.feed, options=dict(video=not self.video_paused)))

    # Chat: the gateway relays it to the room's MSRP switch

    def send_message(self, content, content_type='text/plain'):
        """Send a message to the room's chat; its id (the delivery report comes with it)."""
        if self.connection is None or self.state == 'terminated':
            raise RuntimeError('not in the room')
        message_id = uuid.uuid4().hex
        if isinstance(content, bytes):
            content = content.decode('latin1')      # binary goes as latin1 over JSON, the gateway turns it back
        self.connection.request(dict(sylkrtc='videoroom-message', session=self.id, message_id=message_id, content=content, content_type=content_type))
        return message_id

    # Extra videos

    def add_extra_video(self, source, label):
        """Publish another video of ours (source as the media backend understands it): an ExtraVideo, started
        once the room is established."""
        extra = ExtraVideo(self, source, label)
        if self.state == 'established':
            extra.start()
        else:
            self._pending_extras = getattr(self, '_pending_extras', []) + [extra]
        return extra

    # ICE

    def _trickle(self, session_id, candidates):
        if self.connection is None or self.state == 'terminated':
            return
        if session_id not in self._trickle_ready:
            self._trickle_pending.setdefault(session_id, []).append(candidates)
            return
        self.connection.request(dict(sylkrtc='videoroom-session-trickle', session=session_id, candidates=candidates))

    def _flush_trickle(self, session_id):
        self._trickle_ready.add(session_id)
        for candidates in self._trickle_pending.pop(session_id, []):
            self._trickle(session_id, candidates)

    def _ice_failed(self, label):
        if label == self.id:
            self._terminate('the media connection failed (ICE)')
            return
        extra = self.extra_videos.get(label)
        if extra is not None:
            extra.stop('the media connection failed (ICE)')
            return
        participant = self.feeds.get(label)
        if participant is not None:
            participant.state = 'failed'
            participant._changed()
            self.log(f'media from {participant.uri} failed (ICE)', 'warning')

    def _level(self, label, loud):
        participant = self.feeds.get(label)
        if participant is not None:
            participant.set_level(loud)

    def _media_error(self, source, text, fatal):
        if self.state == 'terminated':
            return
        self.log(f'media error in {source}: {text}', 'error' if fatal else 'warning')
        if fatal:
            self._terminate(f'media error: {text}')

    # Signalling events

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SylkRTCConnectionDidClose(self, notification):
        if self.state != 'terminated':
            self._terminate(f'connection to the server lost: {notification.data.reason}')

    def _NH_SylkRTCConnectionGotEvent(self, notification):
        message = notification.data.message
        if message.get('sylkrtc') != 'videoroom-event':
            return
        if message.get('session') in self.extra_videos:
            self.extra_videos[message['session']]._event(message)
            return
        if message.get('session') != self.id:
            return
        event = message.get('event')
        handler = getattr(self, '_EH_' + (event or '').replace('-', '_'), None)
        if handler is not None:
            try:
                handler(message)
            except Exception as e:
                log.exception(f'{self.room}: handling {event} failed: {e}')

    def _EH_session_state(self, message):
        state = message.get('state')
        if state == 'progress':
            self.log('the server is joining the room')
        elif state == 'accepted':
            self._set_state('accepted')
            self._on_answer(message.get('sdp') or '')
        elif state == 'established':
            self._establish_timer.stop()
            self.log('media established')
            self._set_state('established')
            invitees, self._invitees = self._invitees, []
            if invitees:
                self.invite(invitees)
            extras, self._pending_extras = getattr(self, '_pending_extras', []), []
            for extra in extras:
                extra.start()
        elif state == 'terminated':
            self._terminate(message.get('reason') or 'ended by the server')

    def _EH_initial_publishers(self, message):
        for publisher in message.get('publishers') or []:
            self._add_publisher(publisher)

    _EH_publishers_joined = _EH_initial_publishers

    def _EH_publishers_left(self, message):
        for publisher_id in message.get('publishers') or []:
            participant = self.participants.pop(publisher_id, None)
            if participant is None:
                continue
            self.log(f'{participant.name} left')
            self._remove_feed(participant)
            NotificationCenter().post_notification('VideoroomSessionParticipantDidLeave', sender=self, data=NotificationData(participant=participant))

    def _EH_publisher_updated(self, message):
        participant = self.participants.get(message.get('publisher'))
        if participant is not None:
            participant.update(dict(muted=message.get('muted')))

    def _EH_configure(self, message):
        ids = [str(id) for id in message.get('active_participants') or []]
        originator = message.get('originator')
        who = 'the room' if originator == 'videoroom' else self.speaker_name(str(originator)) if originator else ''
        self.speakers = ids
        self.log(f'speakers: {", ".join(self.speaker_name(id) for id in ids) or "nobody (grid)"}' + (f' (set by {who})' if who else ''))
        NotificationCenter().post_notification('VideoroomSessionSpeakersDidChange', sender=self, data=NotificationData(speakers=list(ids), originator=who))

    def _EH_raised_hands(self, message):
        ids = [str(id) for id in message.get('raised_hands') or []]
        if ids != self.raised_hands:
            for id in (id for id in ids if id not in self.raised_hands):
                self.log(f'{self.speaker_name(id)} raised a hand')
            self.raised_hands = ids
            NotificationCenter().post_notification('VideoroomSessionRaisedHandsDidChange', sender=self, data=NotificationData(raised_hands=list(ids)))

    def _EH_mute_audio(self, message):
        originator = str(message.get('originator') or '')
        if self._muting_everyone or originator == self.id:
            self._muting_everyone = False
            return
        if not self.audio_muted:
            self.set_audio_muted(True, by=self.speaker_name(originator) if originator else 'someone')

    def _EH_mute_request(self, message):
        self.set_audio_muted(bool(message.get('muted')), by='a moderator')

    def _EH_invite_status(self, message):
        uri = str(message.get('participant') or '').replace('sip:', '', 1)
        state = str(message.get('state') or '')
        try:
            code = int(message.get('code') or 0)
        except (TypeError, ValueError):
            code = 0
        reason = str(message.get('reason') or '')
        self.log(f'invite of {uri}: {state}' + (f' ({code} {reason})'.rstrip() if code else ''), 'warning' if state == 'failed' else 'info')
        NotificationCenter().post_notification('VideoroomSessionInviteStatus', sender=self, data=NotificationData(uri=uri, state=state, code=code, reason=reason))

    def _EH_conference_audio_levels(self, message):
        levels = message.get('levels') or []
        if isinstance(levels, dict):
            levels = [dict(entry, participant_id=pid) for pid, entry in levels.items()]
        for entry in levels:
            participant = self.participants.get(str(entry.get('participant_id') or ''))
            if participant is None:
                continue
            level = entry.get('rx_peak') if entry.get('rx_peak') is not None else entry.get('rx', 0)
            try:
                participant.set_level(float(level) > SPEAKING_SIP_LEVEL)
            except (TypeError, ValueError):
                pass

    def _EH_feed_attached(self, message):
        participant = self.feeds.get(message.get('feed'))
        if participant is None:
            return
        sdp = message.get('sdp') or ''
        participant.state = 'connecting'
        participant.video = offers_video(sdp)
        participant._changed()
        self.log(f'offer from {participant.name}: {media_summary(sdp)}')
        sdp_log.info(f'{self.room}: offer from {participant.uri}\n{sdp}')
        try:
            self.media.add_feed(participant, sdp)
        except Exception as e:
            self.log(f'cannot receive {participant.name}: {e}', 'error')
            participant.state = 'failed'
            participant._changed()

    def _EH_feed_established(self, message):
        participant = self.feeds.get(message.get('feed'))
        if participant is not None:
            participant.state = 'established'
            participant._changed()
            self.log(f'receiving {participant.name}')

    def _EH_message(self, message):
        sender = message.get('sender') or {}
        NotificationCenter().post_notification('VideoroomSessionGotMessage', sender=self, data=NotificationData(
            type=message.get('type') or 'normal', content=message.get('content') or '', content_type=message.get('content_type') or 'text/plain',
            sender_uri=(sender.get('uri') or '').replace('sip:', '', 1), sender_name=sender.get('display_name') or '', timestamp=message.get('timestamp')))

    def _EH_message_delivery(self, message):
        NotificationCenter().post_notification('VideoroomSessionDidDeliverMessage', sender=self, data=NotificationData(
            message_id=message.get('message_id'), delivered=bool(message.get('delivered')), code=message.get('code'), reason=message.get('reason')))

    # Feeds

    def _add_publisher(self, publisher):
        publisher_id = publisher.get('id')
        if not publisher_id:
            return
        participant = self.participants.get(publisher_id)
        if participant is not None:
            participant.update(publisher)
            return
        if publisher_id == self.id or publisher_id in self.extra_videos or publisher.get('parent') == self.id:
            return      # ourselves, or one of our extra videos
        participant = Participant(self, publisher)
        participant.is_self = participant.type == 'sip' and participant.uri.lower() in (self.identity.address.lower(), self.address.lower())
        self.participants[publisher_id] = participant
        if participant.visible:
            self.log(f'{participant.name} is in the room ({participant.type})')
        NotificationCenter().post_notification('VideoroomSessionParticipantDidJoin', sender=self, data=NotificationData(participant=participant))
        if participant.subscribable:
            self._attach_feed(participant)

    def _attach_feed(self, participant):
        participant.feed = str(uuid.uuid4())
        participant.state = 'attaching'
        self.feeds[participant.feed] = participant

        def attached(error):
            if error is not None:
                participant.state = 'failed'
                participant._changed()

        self.connection.request(dict(sylkrtc='videoroom-feed-attach', session=self.id, publisher=participant.id, feed=participant.feed), attached)

    def _send_feed_answer(self, participant, sdp):
        if self.connection is None or participant.feed not in self.feeds:
            return
        codec = chosen_video_codec(sdp)
        participant.video_codec = codec[0] if codec else None
        self.connection.request(dict(sylkrtc='videoroom-feed-answer', session=self.id, feed=participant.feed, sdp=sdp))
        self._flush_trickle(participant.feed)
        if self.video_paused:
            self._update_feed_video(participant)

    def _remove_feed(self, participant):
        feed = participant.feed
        if feed is None:
            return
        self.feeds.pop(feed, None)
        self._trickle_ready.discard(feed)
        self._trickle_pending.pop(feed, None)
        if self.connection is not None and self.state != 'terminated':
            self.connection.request(dict(sylkrtc='videoroom-feed-detach', session=self.id, feed=feed))
        try:
            self.media.remove_feed(participant)
        except Exception as e:
            self.log(f'removing the media of {participant.name}: {e}', 'warning')
        participant.feed = None


class ExtraVideo(object):
    """Another video of ours in the room: a publisher of its own, video only, joined with parent
    (our publisher's session id) and label, which the gateway shows under us (feature multiple-video-feeds)."""

    def __init__(self, session, source, label):
        self.session = session
        self.source = source
        self.label = label
        self.id = str(uuid.uuid4())
        self.state = 'idle'         # idle, connecting, established, ended
        self.reason = ''
        self.media = None
        self._join_sent = False
        self._timer = Timer(EXTRA_TIMEOUT, lambda: self._end('no media connection'))

    def __repr__(self):
        return f'<ExtraVideo {self.label} {self.state}>'

    def log(self, text, level='info'):
        getattr(log, level)(f'{self.session.room}: {self.label}: {text}')

    def start(self):
        session = self.session
        if session.connection is None or session.state != 'established':
            raise RuntimeError('the room is not connected')
        if 'multiple-video-feeds' not in session.features:
            self.log('the server does not announce multiple-video-feeds: it may show this video as another participant', 'warning')
        if len(session.extra_videos) >= MAX_EXTRA_VIDEOS:
            self._set_state('ended', f'at most {MAX_EXTRA_VIDEOS} extra videos')
            return
        self._set_state('connecting')
        session.extra_videos[self.id] = self
        try:
            self.media = session.media.create_extra(self)
            self.media.start()
        except Exception as e:
            if self.state != 'ended':
                self.log(f'cannot start: {e}', 'error')
                self._end(f'cannot start: {e}')
            return
        if self.state == 'connecting':
            self._timer.start()

    def stop(self, reason='stopped'):
        if self.state in ('idle', 'ended'):
            return
        connection = self.session.connection
        if connection is not None and connection.state == 'ready' and self._join_sent:
            connection.request(dict(sylkrtc='videoroom-leave', session=self.id))
        self._end(reason)

    def _end(self, reason):
        if self.state == 'ended':
            return
        self._timer.stop()
        self.session.extra_videos.pop(self.id, None)
        self.session._trickle_ready.discard(self.id)
        self.session._trickle_pending.pop(self.id, None)
        media, self.media = self.media, None
        if media is not None:
            try:
                media.stop()
            except Exception as e:
                self.log(f'stopping the media: {e}', 'warning')
        self.log(f'ended: {reason}')
        self._set_state('ended', reason)

    def _set_state(self, state, reason=''):
        self.state = state
        self.reason = reason
        NotificationCenter().post_notification('VideoroomExtraVideoDidChangeState', sender=self, data=NotificationData(state=state, reason=reason))

    # From the media backend (any thread)

    def offer(self, sdp):
        call_in_reactor(self._send_join, sdp)

    def failed(self, reason):
        call_in_reactor(self.stop, reason)

    def _send_join(self, sdp):
        if self.state != 'connecting':
            return
        session = self.session
        if video_ssrc(sdp) is None:
            self.log('the offer has no SSRC: the server may drop the video', 'warning')
        sdp_log.info(f'{session.room}: {self.label}: offer\n{sdp}')
        session.connection.request(dict(sylkrtc='videoroom-join', account=session.address, session=self.id, uri=session.room,
                                        sdp=sdp, audio=False, video=True, parent=session.id, label=self.label), self._join_answered)
        self._join_sent = True
        session._flush_trickle(self.id)

    def _join_answered(self, error):
        if error is not None:
            self._join_sent = False
            self._end(f'the server refused it: {error}')

    def _event(self, message):
        if message.get('event') != 'session-state':
            return
        state = message.get('state')
        if state == 'accepted':
            sdp = message.get('sdp') or ''
            self.log(f'answer: {media_summary(sdp)}')
            try:
                self.media.set_answer(sdp)
            except Exception as e:
                self.stop(f'cannot send: {e}')
        elif state == 'established':
            self._timer.stop()
            self.log('sending')
            self._set_state('established')
        elif state == 'terminated':
            self._join_sent = False
            self._end(message.get('reason') or 'ended by the server')
