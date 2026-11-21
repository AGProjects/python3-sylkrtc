# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""What the sylk command line clients (sylk-conference, sylk-session) share: cameras, statistics,
the console and its log file, and the reports (--discover, --server-info)."""

import logging
import os
import re
import sys
import time

from time import monotonic

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


# Audio devices

class AudioChoice(object):
    """The microphone (input) and speakers (output) to use: the configured one (0: auto, the system default, unless
    configured otherwise), then every device the system has now (1, 2, ...)."""

    TITLES = {'input': 'microphone', 'output': 'speakers'}

    def __init__(self, audio_input, audio_output):
        self.configured = {'input': audio_input or 'auto', 'output': audio_output or 'auto'}
        self.chosen = dict(self.configured)
        self._devices = None        # (time, inputs, outputs), for the panel
        self.on_switch = None       # on_switch(kind): the device of kind chosen anew (one plugged in, or gone)
        self.menu = False           # the device menu (d, in the panel) is open: the arrows move in it
        self.cursor = 0

    def watch(self):
        """The devices looked up again when the system says they changed (not polled): whether it can say."""
        from sylkrtc.gst import watch_audio_devices
        return watch_audio_devices(lambda: reactor.callFromThread(self._changed))

    def _changed(self):
        before = self._devices
        inputs, outputs = self.devices(refresh=True)
        if before is None:
            return
        old = {('input', device.name) for device in before[1]} | {('output', device.name) for device in before[2]}
        new = {('input', device.name) for device in inputs} | {('output', device.name) for device in outputs}
        for kind, name in sorted(new - old):
            log.info(f'audio device added: {name} ({kind})')
        for kind, name in sorted(old - new):
            log.info(f'audio device removed: {name} ({kind})')
        # one plugged in: used (a headset, both ways); the one used gone: back to the configured one
        for kind, devices in (('input', inputs), ('output', outputs)):
            added = [device for device in devices if (kind, device.name) in new - old]
            chosen = self.chosen[kind]
            if added:
                self.chosen[kind] = added[-1]
            elif not isinstance(chosen, str) and (kind, chosen.name) in old - new:
                self.chosen[kind] = self.configured[kind]
            else:
                continue
            if self.on_switch is not None:
                self.on_switch(kind)

    def devices(self, refresh=False):
        """(inputs, outputs): the AudioDevices, as last looked up (again when refresh: on d, v, o and when the system
        says they changed, see watch)."""
        if refresh or self._devices is None:
            from sylkrtc.gst import audio_devices
            inputs, outputs = audio_devices()
            self._devices = (time.monotonic(), inputs, outputs)
        return self._devices[1], self._devices[2]

    def _used(self, kind, device):
        chosen = self.chosen[kind]
        if isinstance(chosen, str):
            return chosen in ('', 'auto') and device.default        # auto: the system's default one
        return chosen.name == device.name

    # macOS names the halves of one device apart: the built-in microphone goes with the built-in speakers, or with
    # the headphones plugged in ('External Headphones'); 'X Microphone' with 'X Speakers'
    _SUFFIX = re.compile(r'^(.*?)\s+(Microphone|Mic|Speakers|Speaker|Headphones|Headset|Input|Output)$', re.I)
    _BUILT_IN = re.compile(r'^(MacBook( Pro| Air)?|iMac( Pro)?|Mac mini|Mac Studio|Mac Pro)\b')
    _EXTERNAL = ('External Headphones', 'External Microphone', 'Headphones', 'Line In', 'Line Out')

    @staticmethod
    def _short(part):
        """'Microphone' mic, 'External Headphones' headphones, 'Speakers' speakers."""
        part = part.replace('External ', '').lower()
        return {'microphone': 'mic', '': 'mic'}.get(part, part)

    @classmethod
    def _base(cls, name):
        match = cls._SUFFIX.match(name)
        return match.group(1) if match else name

    def _groups(self):
        """[(base name, [inputs], [outputs])]: the devices that are halves of one (see above) together, in order."""
        inputs, outputs = self.devices()
        names = [device.name for device in inputs + outputs]
        built_in = next((self._base(name) for name in names if self._BUILT_IN.match(self._base(name))), 'Built-in')
        groups = {}
        for kind, devices in (('input', inputs), ('output', outputs)):
            for device in devices:
                base = built_in if device.name in self._EXTERNAL else self._base(device.name)
                groups.setdefault(base, ([], []))[0 if kind == 'input' else 1].append(device)
        return [(base, ins, outs) for base, (ins, outs) in groups.items()]

    def _sections(self):
        """[(title, [(label, input AudioDevice or None, output AudioDevice or None)])]: each device once, those that
        are both an input and an output (one device, or the halves of one) first, then the inputs, then the outputs."""
        both, only_in, only_out = [], [], []
        for base, ins, outs in self._groups():
            if ins and outs:
                pairs = [(ins[0], output) for output in outs] + [(input, outs[0]) for input in ins[1:]]
                for input, output in pairs:
                    if input.name == output.name:
                        label = input.name
                    else:
                        parts = [self._short(name[len(base):].strip() if name.startswith(base) else name) for name in (input.name, output.name)]
                        label = f'{base} {DIM}({parts[0]} + {parts[1]}){RESET}'
                    both.append((label, input, output))
            else:
                only_in += [(device.name, device, None) for device in ins]
                only_out += [(device.name, None, device) for device in outs]
        return [(title, items) for title, items in (('Combined devices', both), ('Input devices', only_in), ('Output devices', only_out)) if items]

    def _menu_items(self):
        return [item for title, items in self._sections() for item in items]

    def open_menu(self):
        """d: the device menu, at the microphone used."""
        items = self._menu_items()
        self.menu = bool(items)
        self.cursor = next((index for index, (name, input, output) in enumerate(items) if input is not None and self._used('input', input)), 0)

    def menu_key(self, key):
        """A key while the device menu is open: the kinds ('input', 'output') of the device chosen with it ([]: none),
        or None for a key the menu does not take (it closes: the key does what it does elsewhere)."""
        items = self._menu_items()
        if not items:
            self.menu = False
            return None
        self.cursor %= len(items)
        if key in ('up', 'down'):
            self.cursor = (self.cursor + (1 if key == 'down' else -1)) % len(items)
            return []
        if key in ('\n', 'v', 'o'):
            name, input, output = items[self.cursor]
            kinds = []
            if key in ('\n', 'v') and input is not None:
                self.chosen['input'] = input
                kinds.append('input')
            if key in ('\n', 'o') and output is not None:
                self.chosen['output'] = output
                kinds.append('output')
            if key == '\n':
                self.menu = False
            return kinds
        if key == 'left':
            return []               # the menu is the leftmost: ← stays in it
        self.menu = False
        return [] if key in ('d', 'right', '\x1b') else None     # → back to the mixer

    def sidebar(self):
        """The panel's list of the devices (see _sections); IN for the microphone used, OUT for the speakers (in, out
        dim: what else each can be); the device menu's cursor, reversed, when it is open."""
        hint = f'{DIM}↑↓ Enter v o · → back{RESET}' if self.menu else f'{DIM}← or d: choose{RESET}'
        lines = []

        def mark(kind, device):
            # IN / OUT (green): the input / output used; in / out (dim): one it can be; blank: one it cannot
            word = 'in ' if kind == 'input' else 'out'
            return '   ' if device is None else GREEN + BOLD + word.upper() + RESET if self._used(kind, device) else DIM + word + RESET
        index = 0
        for title, items in self._sections():
            lines.append(BOLD + title + RESET + ('  ' + hint if not lines else ''))     # the keys on the first title
            for name, input, output in items:
                label = name
                if self.menu and index == self.cursor:
                    label = REVERSE + BOLD + _ESCAPE.sub('', name) + RESET
                lines.append(f' {mark("input", input)} {mark("output", output)}  {label}')
                index += 1
        return lines

    @staticmethod
    def describe(option):
        if isinstance(option, str):
            return 'system default (auto)' if option in ('', 'auto') else option
        return option.name + (' (system default)' if option.default else '')

    def options(self, kind):
        inputs, outputs = self.devices(refresh=True)
        return [self.configured[kind]] + (inputs if kind == 'input' else outputs)

    def _index(self, options, kind):
        current = self.chosen[kind]
        for index, option in enumerate(options):
            if option is current or (type(option) is type(current) and str(option) == str(current)):
                return index
        return 0

    def current(self, kind):
        return self.describe(self.chosen[kind])

    def lines(self):
        lines = []
        for kind in ('input', 'output'):
            options = self.options(kind)
            chosen = self._index(options, kind)
            lines.append(f'{self.TITLES[kind]}:')
            lines += [f'  {"*" if index == chosen else " "} {index} {self.describe(option)}' for index, option in enumerate(options)]
        return lines

    def select(self, kind, number=None):
        """The option numbered so, or (None) the next one, chosen: it is returned. ValueError for no such number."""
        options = self.options(kind)
        index = (self._index(options, kind) + 1) % len(options) if number is None else number
        if not 0 <= index < len(options):
            raise ValueError(f'no {self.TITLES[kind]} {number} (0-{len(options) - 1})')
        self.chosen[kind] = options[index]
        return options[index]

    def apply(self, media, kind):
        """The chosen one used by media (a RoomMedia or CallMedia, possibly running): a message to show."""
        if media is not None and hasattr(media, 'set_audio_device'):
            try:
                media.set_audio_device(kind, self.chosen[kind])
            except Exception as e:
                return f'{self.TITLES[kind]}: cannot use {self.current(kind)}: {e}'
        return f'{self.TITLES[kind]}: {self.current(kind)}'


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


def meter(level, width=20, floor=-60.0):
    """(RMS dBFS, peak dBFS) as a bar from floor to 0 dBFS (= RMS, | peak) and the numbers."""
    rms, peak = level[0], level[1]
    if peak <= -99:
        return 'silent'
    filled = int(round(width * min(1.0, max(0.0, (rms - floor) / -floor))))
    mark = min(width - 1, int(width * min(1.0, max(0.0, (peak - floor) / -floor))))
    bar = ['='] * filled + [' '] * (width - filled)
    if peak > floor:
        bar[mark] = '|'
    return f'[{"".join(bar)}] rms {rms:.0f} peak {peak:.0f} dBFS'


# The status panel

GREEN, YELLOW, RED, DIM, BOLD, RESET = '\x1b[32m', '\x1b[33m', '\x1b[31m', '\x1b[2m', '\x1b[1m', '\x1b[0m'
REVERSE, CYAN = '\x1b[7m', '\x1b[36m'


def vu(level, width=24, floor=-60.0):
    """A coloured VU meter of (RMS dBFS, peak dBFS): green to -18 dBFS, yellow to -6, red above; | the peak."""
    if level is None:
        return DIM + '·' * width + RESET
    rms, peak = level[0], level[1]
    cells = []
    mark = int(width * min(1.0, max(0.0, (peak - floor) / -floor))) if peak > floor else -1
    for index in range(width):
        top = floor + (index + 1) * -floor / width       # the dBFS this cell stands for
        colour = GREEN if top <= -18 else YELLOW if top <= -6 else RED
        if index == min(mark, width - 1):
            cells.append(colour + BOLD + '|' + RESET)
        elif rms >= top - -floor / width:
            cells.append(colour + '█' + RESET)
        else:
            cells.append(DIM + '·' + RESET)
    return ''.join(cells)


def gauge(value, full, warn, bad, width=10):
    """A coloured bar of value out of full: green, yellow from warn, red from bad; dim dots when unknown."""
    if value is None:
        return DIM + '·' * width + RESET
    filled = min(width, int(round(width * min(1.0, max(0.0, value / full)))))
    colour = RED if value >= bad else YELLOW if value >= warn else GREEN
    return colour + '█' * filled + RESET + DIM + '·' * (width - filled) + RESET


_ESCAPE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def fit(text, columns):
    """text (with colour escapes) cut to columns visible characters."""
    out, visible, position = [], 0, 0
    for match in _ESCAPE.finditer(text):
        plain = text[position:match.start()]
        if visible + len(plain) > columns:
            out.append(plain[:columns - visible])
            return ''.join(out) + RESET
        out.append(plain)
        visible += len(plain)
        out.append(match.group(0))
        position = match.end()
    plain = text[position:]
    out.append(plain[:max(0, columns - visible)])
    return ''.join(out) + RESET


# Vertical meters: groups of columns side by side, each a bar from the bottom (eighth blocks at its top)

EIGHTHS = ' ▁▂▃▄▅▆▇'
VU_FLOOR = -60.0
SLOT = 5                    # a column's width: its bar (3) and the space after it
SPACER = None               # in the groups of meter_panel: the room left, shared between the spacers
GAP = 'gap'                 # in the groups: a few columns of space
SCALE = 'scale'             # in the groups: the dBFS scale of the VU meters that follow


def _column(fraction, height, colour, peak=None, width=3):
    """The rows of a vertical bar, top first: fraction (0-1) of it filled, colour(position 0-1) of each row, a line at
    peak (0-1); a dim track for what is not filled (all of it when fraction is None: not known)."""
    filled = None if fraction is None else min(1.0, max(0.0, fraction)) * height
    peak_row = None if not peak or peak <= 0 else min(height - 1, int(min(1.0, peak) * height - 1e-9))
    rows = []
    for top in range(height):
        index = height - 1 - top
        code = colour((index + 0.5) / height)
        part = int((filled - index) * 8) if filled is not None and index < filled < index + 1 else 0
        if filled is not None and filled >= index + 1:
            rows.append(code + '█' * width + RESET)
        elif part:
            rows.append(code + EIGHTHS[part] * width + RESET)
        elif index == peak_row:
            rows.append(code + BOLD + '━' * width + RESET)
        else:
            rows.append(DIM + '░' * width + RESET)
    return rows


def _vu_fraction(decibels):
    return min(1.0, max(0.0, (decibels - VU_FLOOR) / -VU_FLOOR))


def _vu_colour(position):
    decibels = VU_FLOOR + position * -VU_FLOOR
    return GREEN if decibels <= -18 else YELLOW if decibels <= -6 else RED


def vu_meters(level, channels=1):
    """The meters of a level of media.levels() ((RMS, peak, [(RMS, peak) per channel]) dBFS): one per channel (L and
    R for two); channels of them, unlit, when the level is None."""
    if level is None:
        return [('vu', None, '')] * channels
    per_channel = level[2] if len(level) > 2 and level[2] else [(level[0], level[1])]
    names = ['L', 'R'] if len(per_channel) == 2 else [''] * len(per_channel)
    return [('vu', value, name) for value, name in zip(per_channel, names)]


def gauge_meter(value, full, warn, bad, text):
    """A meter of value out of full (green, yellow from warn, red from bad), text under it."""
    return ('gauge', value, full, warn, bad, text)


def _cells(meter, height):
    if meter[0] == 'vu':
        value, name = meter[1], meter[2]
        if value is None:
            return _column(None, height, _vu_colour), '-'
        rms, peak = value
        text = (name + (f'{rms:.0f}' if peak > -99 else 'off')).strip()
        return _column(_vu_fraction(rms), height, _vu_colour, peak=_vu_fraction(peak) if peak > VU_FLOOR else None), text
    value, full, warn, bad, text = meter[1:]
    return _column(None if value is None else value / full, height,
                   lambda position: RED if position * full >= bad else YELLOW if position * full >= warn else GREEN), text


def _center(text, width):
    text = text[:width]
    left = (width - len(text)) // 2
    return ' ' * left + text + ' ' * (width - len(text) - left)


def meter_panel(height, width, title, groups, info=(), footer=''):
    """The lines of a status panel height lines high: the title, the groups of vertical meters side by side, left
    aligned (a group: (name, [meters]) of vu_meters and gauge_meter, or (name, [meters], style): style True for the
    selected one (reversed), 'muted' for a muted one (in red), the same width either way; GAP: some space; SCALE: the dBFS scale;
    SPACER: the room left, shared between the spacers), the groups' names and every meter's value under them, the
    info lines, the footer."""
    info = list(info)
    rows = height - 4 - len(info)
    while rows < 4 and info:
        info.pop()
        rows += 1
    rows = max(2, rows)
    marks = {}
    for decibels in (0, -6, -18, -30, -45):
        row = int(round((1 - _vu_fraction(decibels)) * (rows - 1)))
        marks.setdefault(row, f'{decibels:>3} ' + DIM + '┤' + RESET)
    built, fixed, spacers = [], 1, 0
    for group in groups:
        if group is SPACER:
            built.append(('spacer',))
            spacers += 1
        elif group == GAP:
            built.append(('space', 4))
            fixed += 4
        elif group == SCALE:
            built.append(('scale',))
            fixed += 5
        else:
            name, meters = group[0], group[1]
            columns = [_cells(meter, rows) for meter in meters]
            group_width = max(len(name) + 2, SLOT * len(columns) + 1)
            built.append(('group', name, columns, group_width, len(group) > 2 and group[2]))
            fixed += group_width
    room = max(0, width - 1 - fixed)
    shares = [room // spacers + (1 if index < room % spacers else 0) for index in range(spacers)] if spacers else []

    def line(part):
        text, spacer = ' ', 0
        for entry in built:
            if entry[0] == 'spacer':
                text += ' ' * shares[spacer]
                spacer += 1
            elif entry[0] == 'space':
                text += ' ' * entry[1]
            elif entry[0] == 'scale':
                text += part('scale', None)
            else:
                text += part('group', entry)
        return text

    def meter_row(row):
        def part(kind, entry):
            if kind == 'scale':
                return marks.get(row, '    ' + DIM + '│' + RESET)
            name, columns, group_width = entry[1:4]
            return ' ' + ''.join(cells[row] + ' ' * (SLOT - 3) for cells, text in columns) + ' ' * (group_width - 1 - SLOT * len(columns))
        return line(part)

    def name_part(kind, entry):
        if kind == 'scale':
            return ' ' * 5
        name, columns, group_width, style = entry[1:]
        if style == 'muted':        # in red, the same width: nothing moves
            return RED + BOLD + _center(name, group_width) + RESET
        if style:
            label = _center(name, group_width - 1)
            return REVERSE + BOLD + label + RESET + ' '
        return BOLD + _center(name, group_width) + RESET

    def value_part(kind, entry):
        if kind == 'scale':
            return ' ' * 5
        name, columns, group_width = entry[1:4]
        return ' ' + ''.join(_center(text, SLOT - 1) + ' ' for cells, text in columns) + ' ' * (group_width - 1 - SLOT * len(columns))

    return [title] + [meter_row(row) for row in range(rows)] + [line(name_part), line(value_part)] + info + [footer]


class MixerControl(object):
    """The console mixer of a media (its mixer_strips(): what we send, then what we hear): left and right select a
    strip, up and down change its gain by STEP dB. The gains are kept for the next call (or connection to the room),
    and for what is heard from a participant that joins later."""

    STEP = 1.0
    KEYS = ('left', 'right', 'up', 'down')

    def __init__(self, hidden=()):
        self.selected = 0
        self.gains = {}             # (kind, key) -> gain, as last set
        self.hidden = set(hidden)   # (kind, key) of strips not shown, nor selected (('send', 'total') by default)
        self.monitors = {}          # strip key -> whether we hear it here, as last set
        self.elsewhere = False      # the arrows are in the device menu: no strip shown selected
        self._media = None

    def strips(self, media):
        if media is None or not hasattr(media, 'mixer_strips'):
            return []
        if media is not self._media:
            self._media = media
            for (kind, key), gain in self.gains.items():
                media.set_strip_gain(kind, key, gain)
            for key, on in self.monitors.items():
                if hasattr(media, 'set_strip_monitor'):
                    media.set_strip_monitor(key, on)
        return [strip for strip in media.mixer_strips() if (strip['kind'], strip['key']) not in self.hidden]

    def at_first(self, media):
        """Whether ← now leaves the mixer (for the device menu, on its left): at its first strip, or no mixer."""
        strips = self.strips(media)
        return not strips or self.selected % len(strips) == 0

    def key(self, media, key):
        """A key for the mixer: whether it took it."""
        strips = self.strips(media)
        if not strips or key not in self.KEYS:
            return False
        self.selected %= len(strips)
        if key in ('left', 'right'):
            self.selected = (self.selected + (1 if key == 'right' else -1)) % len(strips)
            return True
        from sylkrtc.gst import GAIN_FLOOR
        strip = strips[self.selected]
        gain = strip['gain']
        if key == 'up' and gain <= GAIN_FLOOR:
            gain = -40.0 - self.STEP        # from off, straight to something (barely) heard
        media.set_strip_gain(strip['kind'], strip['key'], gain + (self.STEP if key == 'up' else -self.STEP))
        for each in media.mixer_strips():
            if (each['kind'], each['key']) == (strip['kind'], strip['key']):
                self.gains[(strip['kind'], strip['key'])] = each['gain']
        return True

    @staticmethod
    def level_gain(digit):
        """A digit's gain, as a fader's: 0 off, 1 to 9 from -40 dB (barely heard) to 0 dB (unity), 5 dB a step (even
        steps of loudness, as one hears it)."""
        from sylkrtc.gst import GAIN_FLOOR
        if digit == 0:
            return GAIN_FLOOR
        return -40.0 + (digit - 1) * 5.0

    @staticmethod
    def level_digit(gain):
        """The key (0-9) of a gain: the nearest step, 9+ above unity, 0 off."""
        from sylkrtc.gst import GAIN_FLOOR
        if gain <= GAIN_FLOOR:
            return '0'
        if gain > 0.5:
            return '9+'
        return str(min(9, max(1, int(round((gain + 40.0) / 5.0)) + 1)))

    def set_level(self, media, digit):
        """0-9 for the selected strip (see level_gain): the strip, None when there is none."""
        strip = self.selected_strip(media)
        if strip is None:
            return None
        media.set_strip_gain(strip['kind'], strip['key'], self.level_gain(digit))
        for each in media.mixer_strips():
            if (each['kind'], each['key']) == (strip['kind'], strip['key']):
                self.gains[(strip['kind'], strip['key'])] = each['gain']
                return each
        return strip

    def toggle_monitor(self, media):
        """l: hear the selected strip here too, or not: a message to show."""
        strip = self.selected_strip(media)
        if strip is None or strip['kind'] != 'send':
            return 'select one of ours (the microphone, the music) to hear it here'
        if strip['monitor'] is None:
            return 'to hear ourselves here start with --monitor (or --playback), and use headphones'
        on = not strip['monitor']
        media.set_strip_monitor(strip['key'], on)
        self.monitors[strip['key']] = on
        return f'{strip["name"]}: ' + ('heard here too (headphones: from speakers the microphone takes it in again)' if on else 'not heard here')

    def selected_strip(self, media):
        strips = self.strips(media)
        return strips[self.selected % len(strips)] if strips else None

    def label(self, media, kind, key, name, muted=False):
        """(name and gain, the same width whatever the gain; style for meter_panel: True selected, 'muted', False)."""
        from sylkrtc.gst import GAIN_FLOOR
        strips = self.strips(media)
        for index, strip in enumerate(strips):
            if (strip['kind'], strip['key']) == (kind, key):
                gain = strip['gain']
                text = f'{self.level_digit(gain):>2} ' + ('off' if gain <= GAIN_FLOOR else f'{gain:+.0f}dB').rjust(5)     # the key, the dB
                selected = index == self.selected % len(strips) and not self.elsewhere
                if strip.get('monitor') is not None:
                    name = ('● ' if strip['monitor'] else '○ ') + name     # heard here too, or not
                return f'{name} {text}', True if selected else 'muted' if muted else False
        return name, 'muted' if muted else False

    @staticmethod
    def info(mixer):
        if mixer is None or mixer.title is None:
            return None
        return (f' {CYAN}♪{RESET} {mixer.title} {DIM}({mixer.track + 1} of {len(mixer.files)}){RESET}'
                + (f'  {YELLOW}{BOLD}PAUSED{RESET}' if getattr(mixer, 'paused', False) else ''))


def network_lines(rtt, loss_out, loss_in, jitter_out=None, jitter_in=None, jitter=False, width=12, late_in=None, totals=None,
                  server_rtt=None):
    """The panel's lines of the network (on the left, over the devices): round trip time (ms), loss (%) of what we
    send (the far end's report) and of what we get, the packets that came too late for the jitter buffer (%: heard
    as gaps, not loss) - those the worst of the last Statistics.HOLD s, and totals ({name: packets since the start})
    after them -, and with jitter the jitter (ms) both ways, each a bar."""
    totals = totals or {}

    def line(name, value, full, warn, bad, text, key=None):
        total = totals.get(key) if key else None
        return (f'{name:<9}{gauge(value, full, warn, bad, width)} ' + ('-' if value is None else text)
                + (f' {DIM}{total}{RESET}' if total else ''))
    lines = [f'{BOLD}network{RESET}  {DIM}worst of {Statistics.HOLD} s · packets in all{RESET}',
             # the far end's, from its RTCP reports; when they have none, the server's (a WebSocket ping)
             line('rtt', rtt, 500, 150, 300, f'{rtt:.0f} ms' if rtt is not None else '') if rtt is not None or server_rtt is None else
             line('rtt srv', server_rtt, 500, 150, 300, f'{server_rtt:.0f} ms'),
             line('loss ↑', loss_out, 10, 1, 5, f'{loss_out:.1f}%' if loss_out is not None else '', 'lost_out'),
             line('loss ↓', loss_in, 10, 1, 5, f'{loss_in:.1f}%' if loss_in is not None else '', 'lost_in'),
             line('late ↓', late_in, 10, 1, 5, f'{late_in:.1f}%' if late_in is not None else '', 'late_in')]
    if jitter:
        lines += [line('jitter ↑', jitter_out, 100, 30, 60, f'{jitter_out:.0f} ms' if jitter_out is not None else ''),
                  line('jitter ↓', jitter_in, 100, 30, 60, f'{jitter_in:.0f} ms' if jitter_in is not None else '')]
    return lines


def network_text(rtt, loss_out, loss_in):
    """The same in a line, when the panel has no room on the left."""
    def value(number, unit, digits=0):
        return '-' if number is None else f'{number:.{digits}f}{unit}'
    return f' {DIM}rtt{RESET} {value(rtt, " ms")}  {DIM}loss{RESET} ↑{value(loss_out, "%", 1)} ↓{value(loss_in, "%", 1)}'


def visible_length(text):
    return len(_ESCAPE.sub('', text))


def with_sidebar(lines, sidebar, width):
    """The panel's lines with sidebar on the left of all but the title (cut to width, then a rule)."""
    out = lines[:1]
    for index, line in enumerate(lines[1:]):
        side = fit(sidebar[index], width - 2) if index < len(sidebar) else ''
        out.append(side + ' ' * max(0, width - 2 - visible_length(side)) + DIM + '│' + RESET + ' ' + line)
    return out


class StatusPanel(object):
    """A panel fixed at the top of the terminal (its first height lines), the console scrolling under it: an ANSI
    scroll region (DECSTBM), so print and the log go on as before. Only on a terminal; update(lines) redraws it."""

    def __init__(self, height=None, fraction=None, minimum=8):
        self.fixed_height, self.fraction, self.minimum = height, fraction, minimum
        self.height = height or minimum
        self.columns = 80
        self.active = False
        self.lines = []
        self._stream = None
        self._columns = 80
        self.on_resize = None       # called when the terminal changes size, before redrawing: lines for the new size

    def start(self):
        """Whether the panel is shown (not when stdout is no terminal, or too small)."""
        stream = getattr(sys.stdout, 'stream', sys.stdout)      # past the log file copy (_Tee): the panel is not logged
        try:
            if not stream.isatty() or os.environ.get('TERM', 'dumb') == 'dumb':
                return False
        except Exception:
            return False
        self._stream = stream
        if not self._setup(first=True):
            return False
        self.active = True
        import atexit
        import signal
        if hasattr(signal, 'SIGWINCH'):
            signal.signal(signal.SIGWINCH, lambda signum, frame: reactor.callLater(0, self._resized))
        atexit.register(self.stop)
        return True

    def _setup(self, first=False):
        import shutil
        columns, rows = shutil.get_terminal_size((80, 24))
        height = self.fixed_height or max(self.minimum, int(rows * self.fraction))
        height = min(height, rows - 4)          # the console keeps 4 lines at least
        if height < self.minimum:
            return False
        self.height = height
        self._columns, self._rows = columns, rows
        self.columns = columns
        self._write(('\n' * self.height if first else '')         # what is on the screen moves up, out of its way
                    + f'\x1b[{self.height + 1};{rows}r'            # the scroll region: under the panel
                    + f'\x1b[{rows};1H')                           # back at the bottom, where the console goes on
        return True

    def _resized(self):
        if self.active and self._setup():
            self._write('\x1b7' + ''.join(f'\x1b[{index + 1};1H\x1b[2K' for index in range(self.height)) + '\x1b8')
            if self.on_resize is not None:
                self.on_resize()
            self._draw()

    def update(self, lines):
        self.lines = list(lines)
        if self.active:
            self._draw()

    def _draw(self):
        out = '\x1b7'      # the cursor, kept: what is typed, and the log, go on where they were
        for index in range(self.height):
            text = self.lines[index] if index < len(self.lines) else ''
            out += f'\x1b[{index + 1};1H\x1b[2K' + fit(text, self._columns - 1)
        self._write(out + '\x1b8')

    def stop(self):
        if not self.active:
            return
        self.active = False
        self._write(f'\x1b[r\x1b[{self._rows};1H\n')      # the whole screen scrolls again

    def _write(self, text):
        # one write at a time with the log handlers (they write from other threads too), after what they have pending
        handlers = list(logging.getLogger().handlers)
        for handler in handlers:
            handler.acquire()
        try:
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except Exception:
                    pass
            data, fd = text.encode('utf-8'), self._stream.fileno()
            while data:
                try:
                    data = data[os.write(fd, data):]
                except BlockingIOError:
                    import select
                    select.select([], [fd], [], 1)
        except OSError:
            pass
        finally:
            for handler in reversed(handlers):
                handler.release()


class Statistics(object):
    """The traffic of a session's streams (its media's stats(): [(label, direction, stats)]): sampled every
    SAMPLE_INTERVAL s, logged every interval s, summed on leaving (earlier connections of the same run included)."""

    SAMPLE_INTERVAL = 5
    LIVE_INTERVAL = 2
    TICK = 0.25                 # s between measures of the audio levels (self.current: the panel, the live lines)
    STATS_TICK = 1              # s between those of the RTP statistics (webrtcbin's get-stats)
    HOLD = 10                   # s: the panel's loss and late bars show the worst of this long (a short burst stays seen)

    def __init__(self, interval):
        self.interval = interval
        self.session = None
        self.samples = {}           # (label, direction) -> stats, of the current connection
        self.logged = {}            # (label, direction) -> (bytes, time) when last logged
        self.earlier = dict(sent=0, received=0, sent_packets=0, received_packets=0, lost_sent=0, lost_received=0)
        self.started = None
        self._sampler = None
        self._logger = None
        self.live = False
        self._live = None
        self._live_previous = {}    # (label, direction) -> (bytes, packets, lost, time) at the last measure
        self.current = []           # the latest measure: one dict per stream, see measure()
        self._stats_time = None     # when the RTP statistics were last asked for
        self.strip_levels = {}      # the send mixer's strip name -> its level, of the latest measure
        self._ticker = None

    def start(self, session):
        """A connection is established."""
        from sylkrtc.util import RepeatingTimer
        self.session = session
        self.samples, self.logged = {}, {}
        if self.started is None:
            self.started = reactor.seconds()
        self._sampler = self._sampler or RepeatingTimer(self.SAMPLE_INTERVAL, self.sample)
        self._sampler.start()
        self._live_previous, self.current, self._stats_time = {}, [], None
        self._holds = {}
        self._ticker = self._ticker or RepeatingTimer(self.TICK, self.measure)
        self._ticker.start()
        if self.interval and self._logger is None:
            self._logger = RepeatingTimer(self.interval, self.log)
            self._logger.start()

    def ended(self):
        """The connection ended (its media is gone): what it carried is kept for the summary."""
        totals = self._totals(self.samples)
        for key in self.earlier:
            self.earlier[key] += totals[key]
        self.samples, self.session = {}, None
        self.current = []
        if self._ticker is not None:
            self._ticker.stop()

    def stop(self):
        for timer in (self._sampler, self._logger, self._live, self._ticker):
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

    def toggle_live(self):
        """Log every LIVE_INTERVAL s each stream's RTP statistics and audio level, or stop doing so: whether it now does."""
        from sylkrtc.util import RepeatingTimer
        self.live = not self.live
        if self.live:
            self._live_previous = {}
            self._live = self._live or RepeatingTimer(self.LIVE_INTERVAL, self.log_live)
            self._live.start(0)
        elif self._live is not None:
            self._live.stop()
        return self.live

    def measure(self):
        """Every TICK s while connected, self.current: per stream (ours first) a dict of label, direction, bytes,
        rate (bit/s) and pps since the last measure (None at the first), loss (a fraction: the far end's report for
        what we send, ours since the last measure for what we get), jitter and rtt (s), level ((RMS, peak) dBFS since
        the last measure); None where not known."""
        session = self.session
        media = getattr(session, 'media', None)
        if session is None or session.state != 'established' or getattr(media, 'pipeline', None) is None:
            self.current = []
            return
        now = reactor.seconds()
        try:
            levels = media.levels() if hasattr(media, 'levels') else {}
            self.strip_levels = {label: level for (label, direction), level in levels.items() if direction == 'strip'}
            if self._stats_time is not None and now - self._stats_time < self.STATS_TICK - 0.01:
                # the levels only: the statistics are those of the last second
                self.current = [dict(row, level=levels.get((row['label'], row['direction']))) for row in self.current]
                return
            self._stats_time = now
            streams = media.stats()
        except Exception as e:
            log.debug(f'statistics: {e}')
            return
        rows = []
        for label, direction, stats in sorted(streams, key=lambda item: (item[1] != 'out', item[0])):
            key = (label, direction)
            self.samples[key] = stats
            late = stats.get('late_received', 0) if direction == 'in' else 0
            if direction == 'out':
                count, packets, lost = stats['sent_bytes'], stats['sent_packets'], 0
            else:
                count, packets, lost = stats['received_bytes'], stats['received_packets'], stats['lost_received']
            before = self._live_previous.get(key)
            self._live_previous[key] = (count, packets, lost, now, late)
            row = dict(label=label, direction=direction, bytes=count, rate=None, pps=None, loss=None, jitter=None, rtt=None,
                       level=levels.get(key), late=None)
            if before is not None:
                elapsed = max(1e-3, now - before[3])
                new_packets, new_lost = max(0, packets - before[1]), max(0, lost - before[2])
                row['rate'] = 8.0 * max(0, count - before[0]) / elapsed
                row['pps'] = new_packets / elapsed
                if direction == 'in':
                    row['loss'] = new_lost / (new_packets + new_lost) if new_packets + new_lost else 0.0
                    new_late = max(0, late - (before[4] if len(before) > 4 else 0))
                    row['late'] = new_late / new_packets if new_packets else 0.0      # came, but too late: gaps
            if direction == 'out':
                row['loss'] = stats['fraction_lost_sent']
                row['jitter'] = stats.get('jitter_sent')
                row['rtt'] = stats['rtt']
            else:
                row['jitter'] = stats.get('jitter_received')
            rows.append(row)
        # the worst loss and lateness of the last HOLD s (loss_held, late_held), and what was lost and late since the
        # connection started (lost_total, late_total)
        holds = self.__dict__.setdefault('_holds', {})
        for row, (label, direction, stats) in zip(rows, sorted(streams, key=lambda item: (item[1] != 'out', item[0]))):
            for name in ('loss', 'late'):
                history = holds.setdefault((label, direction, name), [])
                if row[name] is not None:
                    history.append((now, row[name]))
                history[:] = [(when, number) for when, number in history if now - when <= self.HOLD]
                row[name + '_held'] = max(number for when, number in history) if history else row[name]
            row['lost_total'] = stats['lost_received'] if direction == 'in' else stats['lost_sent']
            row['late_total'] = stats.get('late_received', 0) if direction == 'in' else None
        self.current = rows

    def log_live(self):
        """r: the latest measure, one line per stream: rate, packets per second, bytes, loss, jitter, round trip time
        and the audio level."""
        for row in self.current:
            part = f'{row["label"]} {row["direction"]}'
            if row['rate'] is not None:
                part += f' {human_rate(row["rate"])} {row["pps"]:.0f} pkt/s'
            part += f' {human_bytes(row["bytes"])}'
            if row['loss'] is not None:
                part += f' loss {100.0 * row["loss"]:.1f}%'
            if row.get('late'):
                part += f' late {100.0 * row["late"]:.1f}%'
            if row['jitter'] is not None:
                part += f' jitter {row["jitter"] * 1000:.0f} ms'
            if row['rtt'] is not None:
                part += f' rtt {row["rtt"] * 1000:.0f} ms'
            if row['level'] is not None:
                part += ' ' + meter(row['level'])
            log.info('rtp: ' + part)

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
        return parts + self._level_lines() + [f'total sent {human_bytes(totals["sent"])} ({human_rate(rates["out"])}), received {human_bytes(totals["received"])} ({human_rate(rates["in"])})'
                        + (f', rtt avg {1000 * sum(rtts) / len(rtts):.0f} ms max {1000 * max(rtts):.0f} ms' if rtts else '')
                        + f', loss sent {loss(totals["lost_sent"], totals["sent_packets"])} received {loss(totals["lost_received"], totals["received_packets"] + totals["lost_received"])}']

    def _level_lines(self):
        """The room's SIP side audio levels, as the server sends them (conference-audio-levels: the conference
        mixer's, 0-127 u-law scale): how many came since the last report, and each participant's latest, rx (what it
        says) and tx (what it hears), mean/peak, and left/right in a stereo room. [] for a session without them."""
        session = self.session
        if session is None or not hasattr(session, 'audio_levels_events'):
            return []
        count = session.audio_levels_events
        before = getattr(self, '_levels_logged', 0)
        self._levels_logged = count
        if not count:
            return ['levels: none received from the server yet (the room\'s SIP side levels)']
        age = monotonic() - session.audio_levels_time if session.audio_levels_time is not None else None
        line = f'levels: {count - before} received' + (f', latest {age:.1f} s ago' if age is not None else '')
        lines = [line]
        for pid, entry in sorted(session.audio_levels.items()):
            participant = session.participants.get(pid)
            name = participant.name if participant is not None else pid[:12]
            part = f'levels: {name} rx {entry.get("rx", "?")}/{entry.get("rx_peak", "?")} tx {entry.get("tx", "?")}/{entry.get("tx_peak", "?")}'
            if entry.get('rx_peak_lr') or entry.get('rx_lr'):
                rx_lr = entry.get('rx_peak_lr') or entry.get('rx_lr')
                tx_lr = entry.get('tx_peak_lr') or entry.get('tx_lr') or ['?', '?']
                part += f' L/R peak rx {rx_lr[0]}/{rx_lr[1]} tx {tx_lr[0]}/{tx_lr[1]}'
            lines.append(part)
        return lines

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

    ARROWS = {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left'}

    def __init__(self, client):
        self.client = client
        self._key_callback = None
        self._saved_tty = None
        self._restore_registered = False
        self._pending = ''          # the start of an escape sequence (an arrow key) that came without its end

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
        # single keys; the arrow keys (ESC [ A..D, or ESC O A..D) as 'up', 'down', 'right', 'left'
        text, self._pending = self._pending + data.decode('utf-8', 'replace'), ''
        index = 0
        while index < len(text):
            callback = self._key_callback
            if callback is None:
                break
            key = text[index]
            index += 1
            if key == '\x1b':
                if len(text) - index < 2 and text[index:] in ('', '[', 'O'):
                    self._pending = key + text[index:]
                    break
                if len(text) - index >= 2 and text[index] in '[O' and text[index + 1] in self.ARROWS:
                    key = self.ARROWS[text[index + 1]]
                    index += 2
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
        # the terminal can be non-blocking (Twisted's stdio sets it for stdin, which shares it): wait instead of losing
        # what does not fit at once (a long SDP)
        rest = text
        while True:
            try:
                self.stream.write(rest)
                break
            except BlockingIOError as e:
                rest = rest[getattr(e, 'characters_written', 0):]
                self._wait()
        self.log_file.record(text, self)
        return len(text)

    def flush(self):
        while True:
            try:
                self.stream.flush()
                return
            except BlockingIOError:
                self._wait()

    def _wait(self):
        try:
            import select
            select.select([], [self.stream.fileno()], [], 1)
        except Exception:
            time.sleep(0.01)

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
