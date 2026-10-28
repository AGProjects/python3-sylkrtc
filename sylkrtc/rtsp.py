# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""Checking that an RTSP camera answers: a DESCRIBE, with Basic or Digest authentication.

probe(url, user, password) connects, asks for the stream's description and returns
a Probe: ok, the status code and reason, the media and codecs of the SDP, the
Server header, and what went wrong. Standard library only; it blocks.
"""

import base64
import hashlib
import re
import socket
import ssl
import time

from urllib.parse import unquote, urlsplit, urlunsplit


__all__ = ['Probe', 'probe']


class Probe(object):
    def __init__(self, url):
        self.url = url
        self.ok = False
        self.code = None
        self.reason = None
        self.server = None
        self.media = []         # [(media, codec)], from the SDP
        self.error = None
        self.time = None        # s to the answer
        self.authentication = None  # Digest or Basic when the camera asked for credentials and took them
        self.sent_credentials = False

    @property
    def summary(self):
        if self.ok:
            media = ', '.join('%s %s' % (kind, codec) for kind, codec in self.media) or 'no media described'
            access = 'credentials accepted (%s)' % self.authentication if self.authentication else 'no credentials asked'
            return 'ok, %s, %s%s (%d ms)' % (access, media, ', %s' % self.server if self.server else '', self.time * 1000)
        if self.code == 401:
            if not self.sent_credentials:
                return 'credentials needed (401): set user and password, or the default ones'
            return 'refused: wrong user or password (401)'
        if self.code == 404:
            return 'no such stream: check the path (404)'
        if self.code is not None:
            return 'answered %d %s' % (self.code, self.reason or '')
        return self.error or 'no answer'


def _read_response(sock):
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError('the connection was closed')
        data += chunk
    head, _, body = data.partition(b'\r\n\r\n')
    lines = head.decode('latin1').split('\r\n')
    match = re.match(r'^RTSP/\d\.\d (\d{3}) ?(.*)$', lines[0])
    if match is None:
        raise ValueError('not an RTSP answer: %s' % lines[0][:80])
    headers = []
    for line in lines[1:]:
        name, _, value = line.partition(':')
        headers.append((name.strip().lower(), value.strip()))
    length = int(next((value for name, value in headers if name == 'content-length'), '0') or 0)
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return int(match.group(1)), match.group(2), headers, body[:length].decode('utf-8', 'replace')


def _authorization(challenges, user, password, uri):
    """The Authorization header for the server's challenges: Digest when offered, else Basic."""
    digest = next((value for value in challenges if value.lower().startswith('digest')), None)
    if digest is not None:
        fields = dict((key.lower(), value) for key, value in re.findall(r'(\w+)="?([^",]*)"?', digest[6:]))
        realm, nonce = fields.get('realm', ''), fields.get('nonce', '')
        ha1 = hashlib.md5(f'{user}:{realm}:{password}'.encode()).hexdigest()
        ha2 = hashlib.md5(f'DESCRIBE:{uri}'.encode()).hexdigest()
        response = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
        return f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}"'
    if any(value.lower().startswith('basic') for value in challenges):
        return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
    return None


def probe(url, user=None, password=None, timeout=5):
    result = Probe(url)
    parts = urlsplit(url)
    if parts.scheme not in ('rtsp', 'rtsps') or not parts.hostname:
        result.error = 'not an rtsp:// address'
        return result
    if parts.username:      # credentials in the address win
        user, password = unquote(parts.username), unquote(parts.password or '')
    netloc = parts.hostname if ':' not in parts.hostname else '[%s]' % parts.hostname
    port = parts.port or (322 if parts.scheme == 'rtsps' else 554)
    uri = urlunsplit((parts.scheme, '%s:%d' % (netloc, port) if parts.port else netloc, parts.path or '/', parts.query, ''))
    start = time.monotonic()
    try:
        sock = socket.create_connection((parts.hostname, port), timeout=timeout)
    except socket.timeout:
        result.error = 'no connection to %s port %d (timeout)' % (parts.hostname, port)
        return result
    except OSError as e:
        result.error = 'no connection to %s port %d (%s)' % (parts.hostname, port, e.strerror or e)
        return result
    try:
        sock.settimeout(timeout)
        if parts.scheme == 'rtsps':
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(sock, server_hostname=parts.hostname)
        authorization = None
        for cseq in (1, 2):
            request = f'DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\nUser-Agent: sylkrtc\r\n'
            if authorization:
                request += f'Authorization: {authorization}\r\n'
            sock.sendall((request + '\r\n').encode())
            code, reason, headers, body = _read_response(sock)
            result.code, result.reason = code, reason
            result.server = next((value for name, value in headers if name == 'server'), None)
            if code == 401 and authorization is None and user:
                authorization = _authorization([value for name, value in headers if name == 'www-authenticate'], user, password or '', uri)
                if authorization:
                    result.authentication = authorization.split()[0]
                    result.sent_credentials = True
                    continue
            break
        result.time = time.monotonic() - start
        if result.code != 200:
            result.authentication = None
        if result.code == 200:
            result.ok = True
            for match in re.finditer(r'^m=(\w+) [^\r\n]*', body, re.M):
                rest = body[match.end():]
                following = re.search(r'^m=', rest, re.M)
                section = rest[:following.start()] if following else rest
                rtpmap = re.search(r'^a=rtpmap:\d+ ([^/\r\n]+)', section, re.M)
                result.media.append((match.group(1), rtpmap.group(1) if rtpmap else '?'))
    except socket.timeout:
        result.error = 'no answer from %s port %d (timeout)' % (parts.hostname, port)
    except (OSError, ValueError) as e:
        result.error = str(e)
    finally:
        sock.close()
    return result
