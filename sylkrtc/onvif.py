# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""Finding IP cameras on the LAN: ONVIF WS-Discovery, then their RTSP address over ONVIF.

discover() sends a WS-Discovery Probe for NetworkVideoTransmitter devices to the
multicast group 239.255.255.250:3702 and collects the ProbeMatches: the address of
each camera's device service (XAddrs) and its scopes (name, hardware, location).

stream_uri() asks a camera's device service for its media service (GetCapabilities),
its profiles (GetProfiles) and the RTSP address of the first (main) one
(GetStreamUri), authenticated with a WS-Security UsernameToken (password digest,
with the camera's clock taken from GetSystemDateAndTime). Cameras that refuse it,
or devices not found by WS-Discovery, can be given a guess for well-known makes.

Only the standard library is used; everything here blocks.
"""

import base64
import hashlib
import os
import re
import socket
import time
import uuid

from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


__all__ = ['Device', 'discover', 'stream_uri', 'guess_stream_uri', 'ONVIFError', 'normalize_mac', 'mac_address', 'host_of_mac', 'onvif_mac', 'with_host']


MULTICAST = ('239.255.255.250', 3702)

NS = {
    'soap': 'http://www.w3.org/2003/05/soap-envelope',
    'wsa': 'http://schemas.xmlsoap.org/ws/2004/08/addressing',
    'd': 'http://schemas.xmlsoap.org/ws/2005/04/discovery',
    'tds': 'http://www.onvif.org/ver10/device/wsdl',
    'trt': 'http://www.onvif.org/ver10/media/wsdl',
    'tt': 'http://www.onvif.org/ver10/schema',
}

PROBE = '''<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:%s</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'''

# the main stream of makes whose RTSP path is well known, by a word in their name or hardware scope
KNOWN_PATHS = [
    ('hikvision', '/Streaming/Channels/101'),
    ('dahua', '/cam/realmonitor?channel=1&subtype=0'),
    ('amcrest', '/cam/realmonitor?channel=1&subtype=0'),
    ('reolink', '/h264Preview_01_main'),
    ('axis', '/axis-media/media.amp'),
]


class ONVIFError(Exception):
    pass


class Device(object):
    """A camera found by WS-Discovery."""

    def __init__(self, address, xaddrs, scopes):
        self.address = address          # the IP address the answer came from
        self.xaddrs = xaddrs            # device service URLs
        self.scopes = scopes
        self.name = self._scope('name')
        self.hardware = self._scope('hardware')
        self.location = self._scope('location')

    def _scope(self, kind):
        for scope in self.scopes:
            match = re.match(r'^onvif://www\.onvif\.org/%s/(.+)$' % kind, scope)
            if match:
                return unquote(match.group(1)).replace('_', ' ').strip() or None
        return None

    @property
    def device_service(self):
        # the one on the address it answered from, when it lists several (IPv6, other interfaces)
        for xaddr in self.xaddrs:
            if urlsplit(xaddr).hostname == self.address:
                return xaddr
        return self.xaddrs[0] if self.xaddrs else None

    @property
    def description(self):
        return ' '.join(part for part in (self.name, self.hardware) if part) or 'camera'

    def __repr__(self):
        return f'<Device {self.address} {self.description}>'


def discover(timeout=3.0, interface=None, target=MULTICAST):
    """The ONVIF cameras that answer a WS-Discovery probe within timeout seconds, one per address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if interface:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        sock.bind(('', 0))
        message = (PROBE % uuid.uuid4()).encode()
        for attempt in range(2):        # UDP: once more, in case the first is lost
            sock.sendto(message, target)
        devices = {}
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, (address, port) = sock.recvfrom(65536)
            except socket.timeout:
                break
            for device in _parse_probe_matches(data, address):
                devices.setdefault(device.address, device)
        return sorted(devices.values(), key=lambda device: socket.inet_aton(device.address))
    finally:
        sock.close()


def _parse_probe_matches(data, address):
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return []
    found = []
    for match in root.iter('{%s}ProbeMatch' % NS['d']):
        xaddrs = (match.findtext('d:XAddrs', '', NS) or '').split()
        scopes = (match.findtext('d:Scopes', '', NS) or '').split()
        if xaddrs:
            found.append(Device(address, xaddrs, scopes))
    return found


# SOAP

def _security_header(user, password, clock_offset):
    nonce = os.urandom(16)
    created = (datetime.now(timezone.utc) + clock_offset).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return ('<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            '<UsernameToken><Username>%s</Username>'
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">%s</Password>'
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary">%s</Nonce>'
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">%s</Created>'
            '</UsernameToken></Security></s:Header>') % (_escape(user), digest, base64.b64encode(nonce).decode(), created)


def _escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _call(url, body, user=None, password=None, clock_offset=timedelta(0), timeout=5):
    header = _security_header(user, password or '', clock_offset) if user else ''
    envelope = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="%s" xmlns:tds="%s" xmlns:trt="%s" xmlns:tt="%s">%s<s:Body>%s</s:Body></s:Envelope>'
                % (NS['soap'], NS['tds'], NS['trt'], NS['tt'], header, body)).encode()
    request = Request(url, data=envelope, headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
    except Exception as e:
        data = getattr(e, 'read', lambda: b'')() if hasattr(e, 'code') else b''
        reason = _fault(data) or str(e)
        raise ONVIFError(reason)
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ONVIFError(f'unreadable answer: {e}')
    fault = root.find('.//soap:Fault', NS)
    if fault is not None:
        raise ONVIFError(_fault(data) or 'fault')
    return root


def _fault(data):
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    text = root.find('.//soap:Fault//soap:Reason/soap:Text', NS)
    if text is not None and text.text:
        return text.text.strip()
    subcode = root.find('.//soap:Fault//soap:Subcode/soap:Value', NS)
    return subcode.text.strip() if subcode is not None and subcode.text else None


def _clock_offset(device_service):
    """How far the camera's clock is from ours (WS-Security tokens carry the time they were made)."""
    try:
        root = _call(device_service, '<tds:GetSystemDateAndTime/>')
        utc = root.find('.//tt:UTCDateTime', NS)
        date, clock = utc.find('tt:Date', NS), utc.find('tt:Time', NS)
        camera = datetime(int(date.findtext('tt:Year', namespaces=NS)), int(date.findtext('tt:Month', namespaces=NS)), int(date.findtext('tt:Day', namespaces=NS)),
                          int(clock.findtext('tt:Hour', namespaces=NS)), int(clock.findtext('tt:Minute', namespaces=NS)), int(clock.findtext('tt:Second', namespaces=NS)),
                          tzinfo=timezone.utc)
        return camera - datetime.now(timezone.utc)
    except Exception:
        return timedelta(0)


def stream_uri(device, user, password, timeout=5):
    """(RTSP address, (width, height) or None) of the camera's first media profile; ONVIFError when it refuses."""
    service = device.device_service
    if service is None:
        raise ONVIFError('no device service address')
    offset = _clock_offset(service)
    root = _call(service, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>', user, password, offset, timeout)
    media = root.findtext('.//tt:Media/tt:XAddr', namespaces=NS)
    if not media:
        raise ONVIFError('the camera has no media service')
    root = _call(media, '<trt:GetProfiles/>', user, password, offset, timeout)
    profiles = root.findall('.//trt:Profiles', NS)
    if not profiles:
        raise ONVIFError('the camera has no media profiles')
    profile = profiles[0]
    size = None
    resolution = profile.find('.//tt:VideoEncoderConfiguration/tt:Resolution', NS)
    if resolution is not None:
        try:
            size = int(resolution.findtext('tt:Width', namespaces=NS)), int(resolution.findtext('tt:Height', namespaces=NS))
        except (TypeError, ValueError):
            size = None
    body = ('<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>'
            '</trt:StreamSetup><trt:ProfileToken>%s</trt:ProfileToken></trt:GetStreamUri>') % _escape(profile.get('token') or '')
    root = _call(media, body, user, password, offset, timeout)
    uri = root.findtext('.//trt:MediaUri/tt:Uri', namespaces=NS)
    if not uri or not uri.startswith(('rtsp://', 'rtsps://')):
        raise ONVIFError('the camera gave no RTSP address')
    return uri.strip(), size


def guess_stream_uri(device):
    """The usual RTSP address of the main stream for well-known makes, or None."""
    text = ' '.join(part for part in (device.name, device.hardware, ' '.join(device.scopes)) if part).lower()
    for make, path in KNOWN_PATHS:
        if make in text:
            return f'rtsp://{device.address}:554{path}'
    return None


# MAC addresses: what identifies a camera when its IP address changes

def normalize_mac(text):
    """aa-bb-cc-dd-ee-ff from any common form (aa:bb:..., AABB.CCDD.EEFF, aabbccddeeff); None when it is not one."""
    digits = re.sub(r'[^0-9a-fA-F]', '', text or '')
    if len(digits) != 12 or re.sub(r'[0-9a-fA-F]', '', re.sub(r'[-:.]', '', text or '')):
        return None
    digits = digits.lower()
    if digits == '000000000000':
        return None
    return '-'.join(digits[i:i + 2] for i in range(0, 12, 2))


def _neighbours():
    """{ip: mac} of the neighbour (ARP) table."""
    table = {}
    try:
        with open('/proc/net/arp') as file:
            next(file)
            for line in file:
                fields = line.split()
                if len(fields) >= 4 and fields[2] != '0x0':
                    mac = normalize_mac(fields[3])
                    if mac:
                        table[fields[0]] = mac
        return table
    except OSError:
        pass
    import subprocess
    try:        # macOS and the BSDs
        output = subprocess.run(['arp', '-an'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for match in re.finditer(r'\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)', output):
        mac = normalize_mac(':'.join(part.zfill(2) for part in match.group(2).split(':')))
        if mac:
            table[match.group(1)] = mac
    return table


def mac_address(host, ports=(554, 80), timeout=1.0):
    """The MAC address of a host on the LAN, from the neighbour table (contacting it first when it is
    not there yet); None for a host behind a router or one that does not answer."""
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return None
    mac = _neighbours().get(ip)
    if mac:
        return mac
    for port in ports:
        try:
            socket.create_connection((ip, port), timeout=timeout).close()
        except OSError:
            pass
        mac = _neighbours().get(ip)
        if mac:
            return mac
    return None


def host_of_mac(mac):
    """The IP address the neighbour table has for a MAC address now, or None."""
    mac = normalize_mac(mac)
    return next((ip for ip, other in _neighbours().items() if other == mac), None) if mac else None


def onvif_mac(device, user, password, timeout=5):
    """The MAC address the camera reports over ONVIF (GetNetworkInterfaces), or None."""
    service = device.device_service
    if service is None:
        return None
    try:
        root = _call(service, '<tds:GetNetworkInterfaces/>', user, password, _clock_offset(service), timeout)
    except ONVIFError:
        return None
    return normalize_mac(root.findtext('.//tt:HwAddress', namespaces=NS))


def with_host(url, host):
    """url with another host, its port, credentials, path and query kept."""
    parts = urlsplit(url)
    netloc = host + (':%d' % parts.port if parts.port else '')
    if parts.username:
        netloc = parts.netloc.rpartition('@')[0] + '@' + netloc
    return parts._replace(netloc=netloc).geturl()
