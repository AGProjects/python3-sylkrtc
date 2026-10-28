# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""SDP and RTP helpers (text only, no media library)."""

import re

from urllib.parse import quote


__all__ = ['media_summary', 'offers_video', 'video_ssrc', 'chosen_video_codec', 'cvo_id', 'with_cvo', 'rtp_extension',
           'mids', 'ice_urls', 'offered_codecs', 'has_media', 'for_sip', 'CVO_URI']


CVO_URI = 'urn:3gpp:video-orientation'


def _sections(sdp):
    """(media, port, payloads, section text) of each m-line."""
    for match in re.finditer(r'^m=(\w+) (\d+) [^ ]+ ?([^\r\n]*)', sdp, re.M):
        following = re.search(r'^m=', sdp[match.end():], re.M)
        section = sdp[match.end():match.end() + following.start()] if following else sdp[match.end():]
        yield match.group(1), match.group(2), match.group(3).split(), section


def _rejected(port, section):
    return port == '0' and 'a=bundle-only' not in section


def media_summary(sdp):
    """'audio OPUS/48000/2, video VP9/90000' from an SDP, for the log."""
    parts = []
    for media, port, payloads, section in _sections(sdp):
        if _rejected(port, section):
            parts.append(f'{media} rejected')
            continue
        codec = None
        if payloads:
            rtpmap = re.search(r'^a=rtpmap:%s ([^\r\n]+)' % re.escape(payloads[0]), section, re.M)
            codec = rtpmap.group(1) if rtpmap else payloads[0]
        parts.append(f'{media} {codec}' if codec else media)
    return ', '.join(parts) or 'no media'


def offers_video(sdp):
    """Whether an offer carries video: an m=video that is not rejected nor inactive."""
    for media, port, payloads, section in _sections(sdp):
        if media != 'video' or _rejected(port, section):
            continue
        if re.search(r'^a=(inactive|recvonly)\s*$', section, re.M):
            continue
        return True
    return False


def offered_codecs(sdp, kind):
    """[(encoding name, payload type, clock rate, channels, fmtp)] of the first m=<kind> of an SDP, in its order
    (retransmission and FEC left out)."""
    for media, port, payloads, section in _sections(sdp):
        if media != kind or _rejected(port, section):
            continue
        found = []
        for pt in payloads:
            rtpmap = re.search(r'^a=rtpmap:%s ([^/\s]+)/(\d+)(?:/(\d+))?' % re.escape(pt), section, re.M)
            if rtpmap is None or rtpmap.group(1).upper() in ('RTX', 'RED', 'ULPFEC', 'FLEXFEC-03'):
                continue
            fmtp = re.search(r'^a=fmtp:%s ([^\r\n]+)' % re.escape(pt), section, re.M)
            found.append((rtpmap.group(1).upper(), int(pt), int(rtpmap.group(2)), int(rtpmap.group(3) or 1), fmtp.group(1).strip() if fmtp else ''))
        return found
    return []


def for_sip(sdp):
    """Our SDP for a call that becomes SIP, shaped like a browser's: what webrtcbin adds and browsers do not is
    taken out, since the gateway's Janus copies it into the plain RTP SDP of the SIP side or chokes on it:
    a=rtcp-mux-only, transport-cc feedback, the bundle-only m-lines of max-bundle (port 0, which SIP reads as
    a rejected stream: given port 9 as browsers do) and webrtcbin's repeated a=ssrc lines."""
    found = []
    seen = set()
    for line in sdp.splitlines(keepends=True):
        text = line.rstrip('\r\n')
        if text.startswith('m='):
            seen = set()
        if text in ('a=rtcp-mux-only', 'a=bundle-only') or (text.startswith('a=rtcp-fb:') and text.split(None, 1)[-1] == 'transport-cc'):
            continue
        if text.startswith('a=ssrc:'):
            if text in seen:
                continue
            seen.add(text)
        found.append(line)
    sections = ''.join(found)
    # an m-line at port 0 that was bundle-only is bundled, not rejected
    for match in reversed(list(re.finditer(r'^m=\w+ 0 ', sdp, re.M))):
        following = re.search(r'^m=', sdp[match.end():], re.M)
        section = sdp[match.start():match.end() + following.start()] if following else sdp[match.start():]
        if 'a=bundle-only' in section:
            line = sdp[match.start():sdp.index('\n', match.start()) + 1]
            sections = sections.replace(line, line.replace(' 0 ', ' 9 ', 1), 1)
    return sections


def has_media(sdp, kind):
    """Whether an SDP has an m=<kind> that is not rejected."""
    return any(media == kind and not _rejected(port, section) for media, port, payloads, section in _sections(sdp))


def video_ssrc(sdp):
    """The first a=ssrc of the m=video section, or None."""
    for media, port, payloads, section in _sections(sdp):
        if media == 'video':
            ssrc = re.search(r'^a=ssrc:(\d+)', section, re.M)
            return int(ssrc.group(1)) if ssrc else None
    return None


def chosen_video_codec(sdp):
    """(encoding name, payload type) of the first codec of m=video in an answer, or None."""
    for media, port, payloads, section in _sections(sdp):
        if media != 'video':
            continue
        if port == '0':
            return None
        for pt in payloads:
            rtpmap = re.search(r'^a=rtpmap:%s ([^/\r\n]+)' % re.escape(pt), section, re.M)
            if rtpmap is not None and rtpmap.group(1).upper() not in ('RTX', 'RED', 'ULPFEC', 'FLEXFEC-03'):
                return rtpmap.group(1).upper(), int(pt)
        return None
    return None


def _video_section(sdp):
    match = re.search(r'^m=video [^\r\n]*', sdp, re.M)
    if match is None:
        return None
    following = re.search(r'^m=', sdp[match.end():], re.M)
    return match.start(), (match.end() + following.start()) if following is not None else len(sdp)


def cvo_id(sdp):
    """The RTP header extension id of the video orientation (CVO) in m=video, or None."""
    section = _video_section(sdp)
    if section is None:
        return None
    match = re.search(r'^a=extmap:(\d+)(?:/\w+)? %s\s*$' % re.escape(CVO_URI), sdp[section[0]:section[1]], re.M)
    return int(match.group(1)) if match else None


def with_cvo(sdp, extension_id):
    """The answer with the CVO extension accepted in m=video, so the server keeps sending it."""
    section = _video_section(sdp)
    if section is None or cvo_id(sdp) is not None:
        return sdp
    start, end = section
    line_end = sdp.index('\n', start) + 1
    newline = '\r\n' if sdp[line_end - 2:line_end] == '\r\n' else '\n'
    return sdp[:line_end] + 'a=extmap:%d %s%s' % (extension_id, CVO_URI, newline) + sdp[line_end:]


def rtp_extension(data, extension_id):
    """The bytes of an RTP header extension element (one or two byte form, RFC 8285), or None."""
    if len(data) < 12 or not data[0] & 0x10:
        return None
    offset = 12 + 4 * (data[0] & 0x0f)
    if len(data) < offset + 4:
        return None
    profile = (data[offset] << 8) | data[offset + 1]
    end = offset + 4 + 4 * ((data[offset + 2] << 8) | data[offset + 3])
    position = offset + 4
    if profile == 0xbede:
        while position < end and position < len(data):
            byte = data[position]
            if byte == 0:
                position += 1
                continue
            element_id, length = byte >> 4, (byte & 0x0f) + 1
            if element_id == 15:
                return None
            if element_id == extension_id:
                return data[position + 1:position + 1 + length]
            position += 1 + length
    elif profile & 0xfff0 == 0x1000:
        while position + 1 < end and position + 1 < len(data):
            element_id = data[position]
            if element_id == 0:
                position += 1
                continue
            length = data[position + 1]
            if element_id == extension_id:
                return data[position + 2:position + 2 + length]
            position += 2 + length
    return None


def mids(sdp):
    """The mid of each m-line, in order."""
    found = []
    for section in re.split(r'^(?=m=)', sdp, flags=re.M)[1:]:
        match = re.search(r'^a=mid:(\S+)', section, re.M)
        found.append(match.group(1) if match else None)
    return found


def ice_urls(ice_servers):
    """(stun server, [turn server URLs]) in GStreamer's form, from a WebRTC iceServers list."""
    stun, turn = None, []
    for server in ice_servers or []:
        if not isinstance(server, dict):
            continue
        urls = server.get('urls') or server.get('url') or []
        if isinstance(urls, str):
            urls = [urls]
        for url in urls:
            if not isinstance(url, str):
                continue
            scheme, _, rest = url.partition(':')
            scheme = scheme.lower()
            rest = rest.lstrip('/')
            if scheme == 'stun' and stun is None:
                stun = 'stun://' + rest.partition('?')[0]
            elif scheme in ('turn', 'turns'):
                host, _, query = rest.partition('?')
                credentials = ''
                if server.get('username'):
                    credentials = '%s:%s@' % (quote(str(server['username']), safe=''), quote(str(server.get('credential', '')), safe=''))
                turn.append('%s://%s%s%s' % (scheme, credentials, host, ('?' + query) if query else ''))
    return stun, turn
