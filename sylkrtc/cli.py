# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""What the sylk command line clients (sylk-conference, sylk-session) share: cameras, statistics,
the console and its log file, and the reports (--discover, --server-info)."""

import logging
import os
import sys
import time

from application.notification import IObserver, NotificationCenter
from twisted.internet import reactor
from twisted.protocols.basic import LineReceiver
from zope.interface import implementer

from sylkrtc import sipconfig


__all__ = ['NO_CAMERA', 'parse_camera', 'camera_source', 'attached_cameras', 'select_cameras', 'addresses',
           'human_bytes', 'human_rate', 'loss', 'Statistics', 'Console', 'start_log_file', 'setup_logging',
           'report_discovery', 'report_server_info']


log = logging.getLogger('sylk')

NO_CAMERA = ('none', 'false', '0', 'no', 'off')


# Cameras

def parse_camera(text):
    """LABEL=SOURCE[,key=value...] given on the command line: a camera dict."""
    label, sep, rest = text.partition('=')
    if not sep or not label.strip() or not rest.strip():
        raise SystemExit(f'{text}: expected LABEL=SOURCE')
    location, *options = rest.split(',')
    camera = dict(label=label.strip())
    for option in options:
        key, _, value = option.partition('=')
        camera[key.strip()] = value.strip()
    location = location.strip()
    if location.startswith(('rtsp://', 'rtsps://')):
        camera['url'] = location
    elif location == 'test' or location.startswith('test:'):
        camera['test'] = location.partition(':')[2] or 'smpte'
    else:
        camera['device'] = location
    return camera


def camera_source(camera):
    """The media's description of a camera dict (configured or from the command line)."""
    common = dict(resolution=camera.get('resolution') or None, fps=camera.get('fps') or None)
    if camera.get('url'):
        from sylkrtc.onvif import normalize_mac
        return dict(kind='rtsp', url=camera['url'], user=camera.get('user') or '', password=camera.get('password') or '',
                    transcode=camera.get('transcode') or 'auto', mac=normalize_mac(camera.get('id')), **common)
    if camera.get('device'):
        return dict(kind='v4l2', device=camera['device'], **common)
    if camera.get('test'):
        return dict(kind='test', pattern=camera['test'], **common)
    raise SystemExit(f'camera {camera["label"]}: no url, device or test given')


def attached_cameras(configuration):
    """The video capture devices of this machine as cameras, external ones first: a device configured as a local
    camera has its name and settings (and is left out when disabled), the others their own name."""
    from sylkrtc import v4l2
    found = []
    for device in v4l2.preferred():
        same = next((camera for camera in configuration.cameras if camera['kind'] == 'local' and camera.get('device')
                     and os.path.realpath(camera['device']) == os.path.realpath(device.node)), None)
        if same is not None:
            if same['enabled']:
                found.append(dict(same, label=same['name']))
            continue
        found.append(dict(kind='local', label=device.name, device=device.path, external=device.external))
    return found


def select_cameras(configuration, rtsp_texts, local_texts, session_cameras=(), session_name=None, default_local=True):
    """The cameras asked for: [(label, source)], local ones first.

    rtsp_texts, local_texts: the -r and -c values (all, a configured camera, LABEL=SOURCE; none for -c).
    session_cameras: the names a session lists. default_local: when no local camera is asked for, the one
    attached (an external one when plugged in, else the built-in one) unless -c none.
    """
    cameras = []

    def configured(name, origin, kind=None):
        camera = configuration.find_camera(name)
        if camera is not None and kind is not None and camera['kind'] != kind:
            raise SystemExit(f'{origin}: {name} is {"an RTSP" if camera["kind"] == "rtsp" else "a local"} camera ({"-r" if camera["kind"] == "rtsp" else "-c"})')
        if camera is None:
            message = f'{origin}: there is no camera {name} (sylk-settings {"-r" if kind == "rtsp" else "-c"} list)'
            if kind is not None:
                raise SystemExit(message)
            log.warning(message)
        elif not camera['enabled']:
            log.info(f'{origin}: camera {camera["name"]} is disabled')
        elif not any(other.get('id') == camera['id'] for other in cameras):
            cameras.append(dict(camera, label=camera['name']))

    for name in session_cameras or ():
        configured(name, f'session {session_name}')
    no_local = any(text.lower() in NO_CAMERA for text in local_texts)
    if no_local:
        cameras[:] = [camera for camera in cameras if camera.get('kind') != 'local' and 'url' in camera]
    for option, kind, texts in (('-r', 'rtsp', rtsp_texts), ('-c', 'local', local_texts)):
        for text in texts:
            if text.lower() in NO_CAMERA:
                continue
            if text.lower() == 'all':
                if kind == 'local':
                    for camera in attached_cameras(configuration):
                        if not any(os.path.realpath(other.get('device') or '') == os.path.realpath(camera['device']) for other in cameras):
                            cameras.append(camera)
                    continue
                for camera in configuration.cameras:
                    if camera['kind'] == kind and camera['enabled']:
                        configured(camera['id'], option, kind)
            elif '=' in text:
                camera = parse_camera(text)
                if ('url' in camera) != (kind == 'rtsp'):
                    raise SystemExit(f'{option} {text}: ' + ('not an rtsp:// address' if kind == 'rtsp' else 'an RTSP camera goes with -r'))
                cameras.append(camera)
            else:
                configured(text, option, kind)
    if default_local and not no_local and not any('url' not in camera and camera.get('kind') != 'rtsp' for camera in cameras):
        attached = attached_cameras(configuration)
        if attached:
            cameras.append(attached[0])
    selected = [(camera['label'], camera_source(camera)) for camera in cameras]
    return sorted(selected, key=lambda item: item[1]['kind'] == 'rtsp')


def take_room_from_cameras(arguments, configuration, target='room'):
    """sylk-conference -r myroom: argparse gives myroom to -r (its value is optional); what is neither a configured
    camera nor LABEL=SOURCE is the room (or the target), and -r alone means all."""
    if getattr(arguments, target) is not None:
        return
    for values in (getattr(arguments, 'rtsp_camera', []), arguments.local_camera):
        if values and values[-1].lower() not in ('all',) + NO_CAMERA and '=' not in values[-1] and configuration.find_camera(values[-1]) is None:
            setattr(arguments, target, values[-1])
            values[-1] = 'all'
            return


def addresses(texts, domain):
    """user or user@domain (domain when missing), from several texts, each possibly comma separated."""
    found = []
    for text in texts:
        for address in (part.strip() for part in text.split(',')):
            if address.startswith('sip:'):
                address = address[4:]
            if not address:
                continue
            if '@' not in address:
                address = f'{address}@{domain}'
            if address not in found:
                found.append(address)
    return found


# Statistics

def human_bytes(count):
    for unit, size in (('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if count >= size:
            return f'{count / size:.1f} {unit}'
    return f'{count} B'


def human_rate(bits_per_second):
    if bits_per_second >= 1000000:
        return f'{bits_per_second / 1000000:.1f} Mbit/s'
    return f'{bits_per_second / 1000:.0f} kbit/s'


def loss(lost, total):
    return f'{100.0 * lost / total:.1f}%' if total else '-'


class Statistics(object):
    """The traffic of a session's streams (its media's stats(): [(label, direction, stats)]): sampled every
    SAMPLE_INTERVAL s, logged every interval s, summed on leaving (earlier connections of the same run included)."""

    SAMPLE_INTERVAL = 5

    def __init__(self, interval):
        self.interval = interval
        self.session = None
        self.samples = {}           # (label, direction) -> stats, of the current connection
        self.logged = {}            # (label, direction) -> (bytes, time) when last logged
        self.earlier = dict(sent=0, received=0, sent_packets=0, received_packets=0, lost_sent=0, lost_received=0)
        self.started = None
        self._sampler = None
        self._logger = None

    def start(self, session):
        """A connection is established."""
        from sylkrtc.util import RepeatingTimer
        self.session = session
        self.samples, self.logged = {}, {}
        if self.started is None:
            self.started = reactor.seconds()
        self._sampler = self._sampler or RepeatingTimer(self.SAMPLE_INTERVAL, self.sample)
        self._sampler.start()
        if self.interval and self._logger is None:
            self._logger = RepeatingTimer(self.interval, self.log)
            self._logger.start()

    def ended(self):
        """The connection ended (its media is gone): what it carried is kept for the summary."""
        totals = self._totals(self.samples)
        for key in self.earlier:
            self.earlier[key] += totals[key]
        self.samples, self.session = {}, None

    def stop(self):
        for timer in (self._sampler, self._logger):
            if timer is not None:
                timer.stop()

    def sample(self):
        session = self.session
        if session is None or session.state != 'established' or getattr(session.media, 'pipeline', None) is None:
            return
        try:
            for label, direction, stats in session.media.stats():
                self.samples[(label, direction)] = stats
        except Exception as e:
            log.debug(f'statistics: {e}')

    @staticmethod
    def _totals(samples):
        totals = dict(sent=0, received=0, sent_packets=0, received_packets=0, lost_sent=0, lost_received=0)
        for (label, direction), stats in samples.items():
            totals['sent'] += stats['sent_bytes']
            totals['received'] += stats['received_bytes']
            totals['sent_packets'] += stats['sent_packets']
            totals['received_packets'] += stats['received_packets']
            totals['lost_sent'] += stats['lost_sent'] or 0
            totals['lost_received'] += stats['lost_received']
        return totals

    def log(self):
        for line in self.lines():
            log.info('stats: ' + line)

    def lines(self):
        """What the streams carry now: one line per stream (rate since the last report, bytes,
        round trip time, loss), then the totals. [] before anything was sampled."""
        self.sample()
        if not self.samples:
            return []
        now = reactor.seconds()
        parts, rates, rtts = [], {'out': 0.0, 'in': 0.0}, []
        for (label, direction), stats in sorted(self.samples.items(), key=lambda item: (item[0][1] != 'out', item[0][0])):
            count = stats['sent_bytes'] if direction == 'out' else stats['received_bytes']
            before, when = self.logged.get((label, direction), (0, self.started if not self.logged else now - self.interval))
            rate = 8.0 * max(0, count - before) / max(1e-3, now - when)
            self.logged[(label, direction)] = (count, now)
            rates[direction] += rate
            part = f'{label} {direction} {human_rate(rate)} {human_bytes(count)}'
            if direction == 'out':
                if stats['rtt'] is not None:
                    part += f' rtt {stats["rtt"] * 1000:.0f} ms'
                    rtts.append(stats['rtt'])
                if stats['fraction_lost_sent'] is not None:
                    part += f' loss {100.0 * stats["fraction_lost_sent"]:.1f}%'
            else:
                part += f' loss {loss(stats["lost_received"], stats["received_packets"] + stats["lost_received"])}'
            parts.append(part)
        totals = self._totals(self.samples)
        return parts + [f'total sent {human_bytes(totals["sent"])} ({human_rate(rates["out"])}), received {human_bytes(totals["received"])} ({human_rate(rates["in"])})'
                        + (f', rtt avg {1000 * sum(rtts) / len(rtts):.0f} ms max {1000 * max(rtts):.0f} ms' if rtts else '')
                        + f', loss sent {loss(totals["lost_sent"], totals["sent_packets"])} received {loss(totals["lost_received"], totals["received_packets"] + totals["lost_received"])}']

    def summary(self):
        """On leaving: everything this run sent and received."""
        self.sample()
        current = self._totals(self.samples)
        totals = {key: self.earlier[key] + current[key] for key in current}
        if self.started is None:
            return
        duration = max(1.0, reactor.seconds() - self.started)
        rtts = [stats['rtt'] for (label, direction), stats in self.samples.items() if direction == 'out' and stats['rtt'] is not None]
        log.info(f'stats summary: {duration / 60:.1f} min, sent {human_bytes(totals["sent"])} (avg {human_rate(8.0 * totals["sent"] / duration)}), '
                 f'received {human_bytes(totals["received"])} (avg {human_rate(8.0 * totals["received"] / duration)}), '
                 f'total {human_bytes(totals["sent"] + totals["received"])}'
                 + (f', rtt {1000 * sum(rtts) / len(rtts):.0f} ms' if rtts else '')
                 + f', loss sent {loss(totals["lost_sent"], totals["sent_packets"])} received {loss(totals["lost_received"], totals["received_packets"] + totals["lost_received"])}')


# The console

class Console(LineReceiver):
    """The lines typed in the terminal, to client.input(line); or, while a question is asked
    (ask_key), single keys without Enter, as sip-audio-session3 reads its y/n."""

    delimiter = os.linesep.encode()

    def __init__(self, client):
        self.client = client
        self._key_callback = None
        self._saved_tty = None
        self._restore_registered = False

    def lineReceived(self, line):
        self.client.input(line.decode('utf-8', 'replace').strip())

    def ask_key(self, callback):
        """Read single keys, not echoed, until callback(key) returns True; lines meanwhile wait."""
        if self._key_callback is None:
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                self._saved_tty = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except Exception:
                self._saved_tty = None      # not a terminal: keys still arrive, after Enter
            if not self._restore_registered:
                import atexit
                atexit.register(self._restore_tty)
                self._restore_registered = True
        self._key_callback = callback
        self.setRawMode()

    def cancel_key(self):
        """Back to lines (the question was answered, or no longer stands)."""
        if self._key_callback is None:
            return
        self._key_callback = None
        self._restore_tty()
        self.setLineMode()

    def rawDataReceived(self, data):
        for key in data.decode('utf-8', 'replace'):
            callback = self._key_callback
            if callback is None:
                break
            if key == '\r':
                key = '\n'     # Enter, as one key
            if callback(key):
                self.cancel_key()

    def connectionLost(self, reason=None):
        self._restore_tty()

    def _restore_tty(self):
        if self._saved_tty is None:
            return
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_tty)
        except Exception:
            pass
        self._saved_tty = None


class _Tee(object):
    """A console stream that is also written, with a timestamp on each line, to the day's log file."""

    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file

    def write(self, text):
        self.stream.write(text)
        self.log_file.record(text, self)
        return len(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class LogFile(object):
    """~/.sylkclient/logs/<name>-YYYY-MM-DD.log: what is shown on the console, and what is typed (as > text)."""

    def __init__(self, folder, name):
        self.folder = folder
        self.name = name
        self.day = None
        self.file = None
        self.starts = {}        # stream -> at the start of a line

    def _open(self):
        day = time.strftime('%Y-%m-%d')
        if day != self.day:
            if self.file is not None:
                self.file.close()
            os.makedirs(self.folder, mode=0o700, exist_ok=True)
            self.file = open(os.path.join(self.folder, f'{self.name}-{day}.log'), 'a', encoding='utf-8')
            self.day = day
        return self.file

    def record(self, text, source=None):
        try:
            file = self._open()
            stamp = time.strftime('%Y-%m-%d %H:%M:%S ')
            start = self.starts.get(source, True)
            for piece in text.splitlines(keepends=True):
                file.write((stamp if start else '') + piece)
                start = piece.endswith('\n')
            self.starts[source] = start
            file.flush()
        except OSError:
            pass

    def typed(self, line):
        """What was typed: the terminal shows it, the program's output would not."""
        self.record(f'> {line}\n', 'input')


def start_log_file(name):
    """Copy the console (stdout, stderr and the log handlers writing to them) to the log file of name."""
    log_file = LogFile(os.path.join(sipconfig.DEFAULT_DIRECTORY, 'logs'), name)
    originals = {sys.stdout: _Tee(sys.stdout, log_file), sys.stderr: _Tee(sys.stderr, log_file)}
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and getattr(handler, 'stream', None) in originals:
            handler.setStream(originals[handler.stream])
    sys.stdout, sys.stderr = originals[sys.stdout], originals[sys.stderr]
    return log_file


def setup_logging(arguments):
    level = logging.DEBUG if arguments.debug else logging.INFO
    logging.basicConfig(level=level, format='%(asctime)s %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    if (getattr(arguments, 'discover', False) or getattr(arguments, 'server_info', False)) and not arguments.debug:
        level = logging.WARNING     # a report: what it prints, warnings and errors only
        logging.getLogger().setLevel(level)
    # the SDP exchanged with --sdp or -v
    logging.getLogger('sylkrtc.sdp').setLevel(logging.INFO if getattr(arguments, 'sdp', False) or arguments.debug else logging.WARNING)
    # the sylkRTC messages only with --trace
    logging.getLogger('sylkrtc.connection').setLevel(logging.DEBUG if arguments.trace else max(level, logging.INFO))


# Reports

def report_discovery(discovery, account, config_dir, domains, room=None):
    """--discover: the domains looked up again, cached and shown; stops the reactor when done."""
    from twisted.internet.defer import gatherResults
    from sylkrtc.rooms import RoomTarget
    print(f'account {account.id} (from {os.path.join(config_dir, "config")})')

    def show(configuration, domain):
        entry = discovery.configurations.get(domain) or {}
        if not configuration:
            print(f'{domain}: no SylkServer configuration')
            return
        print(f'{domain}: {entry.get("configurationUrl")} (fetched {entry.get("fetched")})')
        print(f'  saved in       {os.path.join(discovery.folder, domain, "configuration.json")}')
        target = RoomTarget(room or 'room', domain, configuration)
        print(f'  wsServer       {target.ws_server}')
        print(f'  video rooms    {target.video_uri.partition("@")[2]}')
        print(f'  MSRP switch    {target.sip_uri.partition("@")[2]}')
        print(f'  guest domain   {target.guest_domain}')
        print(f'  ICE servers    {", ".join(str(url) for server in target.ice_servers for url in (lambda u: [u] if isinstance(u, str) else u)(server.get("urls") or server.get("url") or [])) or "none"}')
        print(f'  keys           {", ".join(sorted(configuration))}')

    gatherResults([discovery.discover(domain).addCallback(show, domain) for domain in domains]).addBoth(lambda result: reactor.stop())


@implementer(IObserver)
class _ServerInfoObserver(object):
    """What the gateway says once connected, then whether it takes the account."""

    def __init__(self, connection, account, own_domain):
        self.connection = connection
        self.account = account
        self.own_domain = own_domain

    def handle_notification(self, notification):
        from sylkrtc.videoroom import Identity
        connection = self.connection
        if notification.name == 'SylkRTCConnectionDidBecomeReady':
            print(f'Server           {connection.server or "not announced"}')
            print(f'Protocol         {connection.subprotocol or "none"}, ready in {connection.connect_time * 1000:.0f} ms')
            print(f'Features         {", ".join(sorted(connection.features)) or "none announced (an older gateway)"}')
            certificate = connection.peer_certificate
            if certificate is not None:
                subject = dict((key.decode(), value.decode()) for key, value in certificate.get_subject().get_components())
                issuer = dict((key.decode(), value.decode()) for key, value in certificate.get_issuer().get_components())
                expires = certificate.get_notAfter().decode()
                print(f'TLS certificate  {subject.get("CN")}, issued by {issuer.get("O") or issuer.get("CN")}, '
                      f'expires {expires[0:4]}-{expires[4:6]}-{expires[6:8]}' + (' (expired)' if certificate.has_expired() else ''))
            from sylkrtc.gst import media_missing, media_report
            missing = media_missing()
            print('Local media      ' + media_report() + (', missing ' + ', '.join(f'{what} ({package})' for what, package in missing) if missing else ''))
            identity = Identity.from_credentials(self.account.id, self.account.password, self.account.display_name, auth_username=self.account.auth_username)
            connection.add_identity(identity.address, identity.password, identity.display_name, self._added)
        elif notification.name == 'SylkRTCConnectionDidClose':
            if connection.connect_time is None and reactor.running:
                print(f'Connection       failed: {notification.data.reason}')
            if reactor.running:
                reactor.stop()

    def _added(self, error):
        if error is None:
            print(f'Account          {self.account.id}: domain accepted' + ('' if self.own_domain else ' (another SylkServer that takes our domain)'))
        else:
            print(f'Account          {self.account.id}: refused ({error})' + ('' if self.own_domain else '; rooms here are joined as a guest'))
        self.connection.close('done')


def report_server_info(discovery, account, config_dir, domain, user_agent, verify_tls):
    """--server-info: connect to the domain's gateway and report what it is and does; stops the reactor when done."""
    from sylkrtc.connection import Connection
    from sylkrtc.rooms import RoomTarget

    def configured(configuration):
        if not configuration or not configuration.get('wsServer'):
            print(f'{domain}: no SylkServer configuration with a wsServer')
            reactor.stop()
            return
        entry = discovery.configurations.get(domain) or {}
        target = RoomTarget('room', domain, configuration)
        print(f'Account          {account.id} (from {os.path.join(config_dir, "config")})')
        print(f'Domain           {domain}')
        print(f'Configuration    {entry.get("configurationUrl")} (fetched {entry.get("fetched")})')
        print(f'Saved in         {os.path.join(discovery.folder, domain, "configuration.json")}')
        print(f'WebSocket        {target.ws_server}')
        print(f'Video rooms      {target.video_uri.partition("@")[2]}')
        print(f'MSRP switch      {target.sip_uri.partition("@")[2]}')
        print(f'Guest domain     {target.guest_domain}')
        for server in target.ice_servers:
            urls = server.get('urls') or server.get('url') or []
            print(f'ICE server       {", ".join([urls] if isinstance(urls, str) else urls)}' + (' (with credentials)' if server.get('username') else ''))
        connection = Connection(target.ws_server, user_agent=user_agent, verify_tls=verify_tls)
        NotificationCenter().add_observer(_ServerInfoObserver(connection, account, domain == account.domain), sender=connection)
        connection.open()

    discovery.discover(domain).addCallback(configured)
