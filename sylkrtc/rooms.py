# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""Room addresses.

A room is given as a link (https://host/conference/room, as the Sylk web app shares
them), as room@host, or as a bare room name (a room of the account's own domain).
The host's SylkServer configuration says where its server is: wsServer, iceServers,
defaultConferenceDomain (its video rooms, videoconference.<domain> unless it says
otherwise) and defaultGuestDomain. The same room's MSRP switch is always
room@conference.<domain>.
"""

import re

from urllib.parse import unquote, urlsplit


__all__ = ['parse_room', 'configuration_domain', 'RoomTarget']


def parse_room(text):
    """(room, host) of what was typed or pasted: a link https://host/conference/room, sip:room@host,
    room@host; host is None for a bare room name; None when there is nothing."""
    text = (text or '').strip()
    if not text:
        return None
    if re.match(r'^https?://', text, re.IGNORECASE):
        parts = urlsplit(text)
        segments = [segment for segment in parts.path.split('/') if segment]
        if not segments or not parts.hostname:
            return None
        room = segments[segments.index('conference') + 1] if 'conference' in segments[:-1] else segments[-1]
        return unquote(room).strip(), parts.hostname.lower()
    text = re.sub('^sips?:', '', text, flags=re.IGNORECASE)
    room, _, host = text.partition('@')
    host = host.partition(';')[0].partition(':')[0].strip().lower()
    return room.strip(), host or None


def configuration_domain(host):
    """The domain whose SylkServer configuration covers a room host."""
    host = host.lower()
    for prefix in ('videoconference.', 'conference.'):
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


class RoomTarget(object):
    """Where a room of a SylkServer domain is, from that domain's configuration."""

    def __init__(self, room, domain, configuration):
        self.room = room
        self.domain = domain
        self.ws_server = configuration.get('wsServer')
        self.ice_servers = configuration.get('iceServers') or []
        video_domain = str(configuration.get('defaultConferenceDomain') or 'videoconference.%s' % domain).lower()
        self.video_uri = '%s@%s' % (room, video_domain)
        sip_domain = 'conference.' + video_domain[len('videoconference.'):] if video_domain.startswith('videoconference.') else 'conference.%s' % domain
        self.sip_uri = '%s@%s' % (room, sip_domain)         # the MSRP switch of the same room
        self.guest_domain = configuration.get('defaultGuestDomain') or 'guest.%s' % domain

    def __repr__(self):
        return f'<RoomTarget {self.video_uri} / {self.sip_uri} at {self.ws_server}>'
