# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""The media of a video room, and of a 1 to 1 call (CallMedia), with GStreamer webrtcbin.

One pipeline per room, so the echo canceller sees what is played:

    microphone -> [webrtcdsp] -> volume (mute) -> opusenc -> rtpopuspay ----------> webrtcbin (publisher)
    webrtcbin (feed) -> decodebin -> audio: convert/resample -> audiomixer -> [webrtcechoprobe] -> speakers
                                  -> video: the video_sink given, else not decoded at all

The publisher sends audio only; other videos of ours are extra videos (ExtraMedia),
each a pipeline and a publisher of its own:

    rtspsrc -> tee -+-> (H264 room, H264 camera) rtph264depay -> h264parse -> rtph264pay ----------------------> webrtcbin
                    +-> (else) decodebin -> convert/scale/rate -> encoder -> payloader -------------------------> webrtcbin
    v4l2src / videotestsrc -> decodebin -> convert/scale/rate -> encoder -> payloader --------------------------> webrtcbin

An RTSP H264 camera is relayed as it is when the room's codec is H264 (no decoding:
what a Raspberry Pi can afford); its key frame interval is then the camera's own, so
it should be short (1-2 s) for others to see it soon after they join.

No GLib main loop is needed: bus messages are taken as they are posted, webrtcbin
and libnice run their own threads. What comes back goes to the session through its
thread safe entry points.
"""

import logging
import math
import os
import random
import re
import threading
import time

from sylkrtc.sdp import chosen_video_codec, complete_answer, cvo_id, has_media, ice_urls, mids, offered_codecs, with_cvo, with_opus_stereo


__all__ = ['MediaFactory', 'RoomMedia', 'ExtraMedia', 'CallMedia', 'VideoFrame', 'media_missing', 'media_report', 'AudioDevice', 'audio_devices', 'watch_audio_devices', 'SendMixer', 'playlist', 'GAIN_FLOOR', 'RESOLUTIONS', 'parse_resolution', 'webrtcbin_stats']


log = logging.getLogger('sylkrtc.gst')

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstWebRTC', '1.0')
    gi.require_version('GstSdp', '1.0')
    from gi.repository import Gst, GstSdp, GstWebRTC
except (ImportError, ValueError) as e:
    Gst = GstSdp = GstWebRTC = None
    _gi_error = str(e)
else:
    _gi_error = None
try:
    gi.require_version('GstRtp', '1.0')
    from gi.repository import GstRtp
except (ImportError, ValueError, NameError):
    GstRtp = None


# What a publisher can offer, in order of preference: (encoding name, payload type, encoder, payloader)
VIDEO_CODECS = [
    ('VP9', 98, 'vp9enc', 'rtpvp9pay'),
    ('VP8', 96, 'vp8enc', 'rtpvp8pay'),
    ('H264', 102, 'x264enc', 'rtph264pay'),
    ('H264', 102, 'openh264enc', 'rtph264pay'),
]
H264_FMTP = 'packetization-mode=(string)1,profile-level-id=(string)42e01f,level-asymmetry-allowed=(string)1'
OPUS_PT = 111

# what an extra video is sent at when it is encoded here: name -> (width, height, kbit/s)
RESOLUTIONS = {'1280x720': (1280, 720, 1200), '640x360': (640, 360, 500), '320x180': (320, 180, 200)}
DEFAULT_RESOLUTION = '640x360'
DEFAULT_FPS = 10

# received audio waits in its queue for the pipeline latency (jitter buffer, mixer, sink buffer): a short
# leaky queue drops what does not fit, a gap every time it fills
RECEIVED_AUDIO_QUEUE = 2            # s
SPEAKING_DB = -38               # dBFS peak of decoded audio above which a participant speaks
LEVEL_INTERVAL = 50 * 1000000   # ns between level messages (the panel shows them every 250 ms)

REQUIRED_ELEMENTS = ['webrtcbin', 'nicesrc', 'decodebin', 'audioconvert', 'audioresample', 'audiomixer', 'volume',
                     'opusenc', 'rtpopuspay', 'fakesink', 'tee', 'queue']

ELEMENT_PACKAGES = {
    'webrtcbin': 'gstreamer1.0-plugins-bad', 'webrtcdsp': 'gstreamer1.0-plugins-bad', 'webrtcechoprobe': 'gstreamer1.0-plugins-bad',
    'openh264enc': 'gstreamer1.0-plugins-bad', 'nicesrc': 'gstreamer1.0-nice', 'x264enc': 'gstreamer1.0-plugins-ugly',
    'avdec_h264': 'gstreamer1.0-libav', 'autoaudiosrc': 'gstreamer1.0-plugins-good', 'autoaudiosink': 'gstreamer1.0-plugins-good',
    'rtspsrc': 'gstreamer1.0-plugins-good', 'v4l2src': 'gstreamer1.0-plugins-good', 'rtpopuspay': 'gstreamer1.0-plugins-good',
    'vp8enc': 'gstreamer1.0-plugins-good', 'vp9enc': 'gstreamer1.0-plugins-good', 'rtpvp8pay': 'gstreamer1.0-plugins-good',
    'rtpvp9pay': 'gstreamer1.0-plugins-good', 'rtph264pay': 'gstreamer1.0-plugins-good', 'rtph264depay': 'gstreamer1.0-plugins-good',
    'h264parse': 'gstreamer1.0-plugins-bad',
}


def _init():
    if _gi_error is not None:
        return False
    if not Gst.is_initialized():
        Gst.init([])
    return True


def _has(factory):
    return Gst.ElementFactory.find(factory) is not None


def media_missing():
    """What is missing, as a list of (what, Debian package); empty when everything is there."""
    if not _init():
        return [(f'GStreamer introspection ({_gi_error})', 'gir1.2-gst-plugins-bad-1.0')]
    missing = [(name, ELEMENT_PACKAGES.get(name, 'gstreamer1.0-plugins-base')) for name in REQUIRED_ELEMENTS if not _has(name)]
    return missing


def media_report():
    """One line about the media that can be done here."""
    if not _init():
        return f'no GStreamer WebRTC ({_gi_error})'
    encoders = ['%s (%s)' % (name, encoder) for name, pt, encoder, payloader in VIDEO_CODECS if _has(encoder) and _has(payloader)]
    return ', '.join(['GStreamer %s' % Gst.version_string().replace('GStreamer ', ''),
                      'video %s' % (', '.join(encoders) or 'none'),
                      'H264 relay %s' % ('yes' if _has('rtph264depay') and _has('h264parse') else 'no'),
                      'audio OPUS',
                      'echo cancellation %s' % ('webrtcdsp' if _has('webrtcdsp') and _has('webrtcechoprobe') else 'none')])


class AudioDevice(object):
    """A microphone or speakers the system has: name, whether it is the system default, and the element using it."""

    def __init__(self, device):
        self.device = device
        self.name = device.get_display_name()
        self.default = False
        properties = device.get_properties()
        if properties is not None and properties.has_field('is-default'):
            try:
                self.default = bool(properties.get_value('is-default'))
            except (TypeError, ValueError):
                pass

    def element(self, name):
        element = self.device.create_element(name)
        if element is None:
            raise RuntimeError(f'cannot use {self.name}')
        return element

    def __str__(self):
        return self.name


class _DeviceWatch(object):
    """A device monitor kept running: the system tells it when audio devices come and go (its bus, read in the
    thread that posts to it: no main loop), and it tells the listeners; get_devices() is then what it knows."""

    def __init__(self):
        self.monitor = None
        self.listeners = []

    def start(self):
        if self.monitor is None and _init():
            monitor = Gst.DeviceMonitor.new()
            monitor.add_filter('Audio/Source', None)
            monitor.add_filter('Audio/Sink', None)
            monitor.get_bus().set_sync_handler(self._on_message, None)
            if monitor.start():
                self.monitor = monitor
        return self.monitor is not None

    def _on_message(self, bus, message, data):
        # the device provider's thread
        if message.type in (Gst.MessageType.DEVICE_ADDED, Gst.MessageType.DEVICE_REMOVED, Gst.MessageType.DEVICE_CHANGED):
            for listener in list(self.listeners):
                try:
                    listener()
                except Exception as e:
                    log.debug(f'audio devices: {e}')
        return Gst.BusSyncReply.DROP


_device_watch = _DeviceWatch()


def watch_audio_devices(listener):
    """listener() (called from any thread) whenever the system's audio devices change; whether the system can tell."""
    if not _device_watch.start():
        return False
    _device_watch.listeners.append(listener)
    return True


def audio_devices():
    """([inputs], [outputs]): the AudioDevices the system has now (those the device watch knows, once it runs)."""
    if not _init():
        return [], []
    if _device_watch.monitor is not None:
        devices = _device_watch.monitor.get_devices() or []
    else:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter('Audio/Source', None)
        monitor.add_filter('Audio/Sink', None)
        started = monitor.start()
        try:
            devices = monitor.get_devices() or []
        finally:
            if started:
                monitor.stop()
    found = {'Audio/Source': [], 'Audio/Sink': []}
    for device in devices:
        for kind, chosen in found.items():
            if device.has_classes(kind) and device.get_display_name() not in [each.name for each in chosen]:
                chosen.append(AudioDevice(device))
    for chosen in found.values():
        chosen.sort(key=lambda each: (not each.default, each.name.lower()))
    return found['Audio/Source'], found['Audio/Sink']


def _replace_audio(media, kind, description):
    """Use other speakers (kind output) or another microphone (input) in media's pipeline, running or not:
    description is auto, none, a GStreamer description or an AudioDevice."""
    sink = kind == 'output'
    current = getattr(media, 'audio_output' if sink else 'audio_input', None)
    if isinstance(description, AudioDevice) and isinstance(current, AudioDevice) and description.name == current.name:
        return          # the one used already: nothing to replace
    setattr(media, 'audio_output' if sink else 'audio_input', description)
    pipeline = media.pipeline
    if pipeline is None:
        return
    name = 'speakers' if sink else 'microphone'
    old = pipeline.get_by_name(name)
    if old is None:
        raise RuntimeError(f'no {name} in the pipeline')
    new = RoomMedia._audio_element(media, description, name, sink)
    channels = pipeline.get_by_name('micchannels') if not sink else None
    if channels is not None:
        channels.set_property('caps', _source_caps(description))      # a mono device mono, a stereo one as it is
    if sink:
        if new.find_property('async') is not None:
            new.set_property('async', False)        # no preroll to wait for in a playing pipeline
        old_pad = old.get_static_pad('sink')
        upstream = old_pad.get_peer()
        if upstream is None:
            raise RuntimeError(f'the {name} are not linked')

        def retire():
            # another thread: stopping an audio sink from the thread that feeds it can block it for good (macOS)
            old.set_state(Gst.State.NULL)
            # the latency told anew: a sink added to a live pipeline does not have it, takes every buffer as late
            # and plays nothing
            pipeline.recalculate_latency()

        def idle(pad, info):
            # the thread that pushes to the speakers, between buffers (or right now): relinked, nothing stopped here
            pad.unlink(old_pad)
            pipeline.remove(old)            # still running, out of the way (the new ones take its name), stopped in retire
            pipeline.add(new)
            result = pad.link(new.get_static_pad('sink'))
            if result != Gst.PadLinkReturn.OK:
                log.warning(f'the new speakers: cannot link them ({result.value_nick})')
            new.sync_state_with_parent()
            threading.Thread(target=retire, name='retire-speakers', daemon=True).start()
            return Gst.PadProbeReturn.REMOVE
        upstream.add_probe(Gst.PadProbeType.IDLE, idle)
    else:
        old_pad = old.get_static_pad('src')
        downstream = old_pad.get_peer()
        old.set_state(Gst.State.NULL)       # a source: its own thread stops, nothing downstream is flushed
        if downstream is not None:
            old_pad.unlink(downstream)
        pipeline.remove(old)
        pipeline.add(new)
        if downstream is not None:
            new.get_static_pad('src').link(downstream)
        new.sync_state_with_parent()
        pipeline.recalculate_latency()      # the new microphone's latency too


def parse_resolution(text):
    """(width, height, kbit/s) of '640x360' (one of RESOLUTIONS, or any WxH)."""
    text = (text or DEFAULT_RESOLUTION).strip().lower()
    if text in RESOLUTIONS:
        return RESOLUTIONS[text]
    match = re.match(r'^(\d+)x(\d+)$', text)
    if match is None:
        raise ValueError(f'not a resolution: {text}')
    width, height = int(match.group(1)), int(match.group(2))
    return width, height, max(150, width * height * 500 // (640 * 360))


def _make(factory, name=None, **properties):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f'GStreamer element {factory} is missing (install {ELEMENT_PACKAGES.get(factory, "gstreamer1.0-plugins-base")})')
    for key, value in properties.items():
        key = key.replace('_', '-')
        if element.find_property(key) is None:
            continue    # a property this version does not have
        if isinstance(value, (str, bool, int, float)):
            # strings take enum and flags nicks; one path for every type keeps versions apart
            Gst.util_set_object_arg(element, key, str(value).lower() if isinstance(value, bool) else str(value))
        else:
            element.set_property(key, value)
    return element


def _channels(caps):
    """The channel count of decoded audio caps, None when not fixed yet."""
    try:
        structure = caps.get_structure(0)
        if structure.has_field('channels'):
            value = structure.get_value('channels')
            return value if isinstance(value, int) else None
    except Exception:
        pass
    return None


def _as_channels(level, channels):
    """A level of _Levels.take with only the first channels channels: the decoded stream had that many, the others
    are its copies (the audio is made stereo for the mixer and the speakers)."""
    if level is None or not channels or len(level[2]) <= channels:
        return level
    kept = level[2][:channels]
    return (max(rms for rms, peak in kept), max(peak for rms, peak in kept), kept)


def _level(name):
    """A level element measuring the audio going through it every LEVEL_INTERVAL ([] without the level plugin)."""
    return [_make('level', name, interval=LEVEL_INTERVAL, post_messages=True)] if _has('level') else []


class _Levels(object):
    """The audio levels the level elements post (any thread), per channel: RMS averaged (in power) and peak maxed
    until taken."""

    def __init__(self):
        self._lock = threading.Lock()
        self._levels = {}       # element name -> [[power sum per channel], count, [peak max per channel]]

    def add(self, structure, name):
        """A bus message's structure: the peak (dBFS, the loudest channel) when it is a level one, else None."""
        if structure is None or structure.get_name() != 'level':
            return None
        try:
            rms = [max(-100.0, value) for value in (structure.get_value('rms') or [])] or [-100.0]
            peak = [max(-100.0, value) for value in (structure.get_value('peak') or [])] or [-100.0]
        except (TypeError, ValueError):
            return None
        channels = min(len(rms), len(peak))
        with self._lock:
            entry = self._levels.get(name)
            if entry is None or len(entry[0]) != channels:
                entry = self._levels[name] = [[0.0] * channels, 0, [-100.0] * channels]
            for channel in range(channels):
                entry[0][channel] += 10 ** (rms[channel] / 10)
                entry[2][channel] = max(entry[2][channel], peak[channel])
            entry[1] += 1
        return max(peak)

    def take(self, name):
        """(RMS dBFS, peak dBFS, [(RMS dBFS, peak dBFS) per channel]) since the last take, None when nothing was
        measured: the first two of all channels (power averaged, peak maxed)."""
        with self._lock:
            entry = self._levels.pop(name, None)
        if not entry or not entry[1]:
            return None

        def decibels(power):
            return 10 * math.log10(power) if power > 1e-10 else -100.0
        powers = [total / entry[1] for total in entry[0]]
        channels = [(decibels(power), peak) for power, peak in zip(powers, entry[2])]
        return (decibels(sum(powers) / len(powers)), max(entry[2]), channels)


# Stereo audio (music: a stereo room, a stereo call): Opus in stereo at this
# bitrate, tuned for any audio, no echo canceller (it takes mono)
STEREO_BITRATE = 128000


def _opus_encoder(stereo):
    if stereo:
        return _make('opusenc', bitrate=STEREO_BITRATE, inband_fec=True, audio_type='generic')
    return _make('opusenc', bitrate=32000, inband_fec=True, audio_type='voice')


def _opus_caps(pt, stereo):
    """The caps of our Opus RTP, stereo=1;sprop-stereo=1 when stereo (webrtcbin puts them in the fmtp)."""
    caps = f'application/x-rtp,media=audio,encoding-name=OPUS,payload={pt},clock-rate=48000,encoding-params=(string)2'
    if stereo:
        caps += ',stereo=(string)1,sprop-stereo=(string)1'
    return Gst.Caps.from_string(caps)


# The level of our microphone in what we send (RFC 6464 ssrc-audio-level): Janus' talking events, the meter of
# our tile in the video room and in the gateway's admin page, come from it
SSRC_AUDIO_LEVEL_URI = 'urn:ietf:params:rtp-hdrext:ssrc-audio-level'
SSRC_AUDIO_LEVEL_ID = 1


_audio_level_reported = False


def _audio_level_missing():
    """What GStreamer lacks to send the ssrc-audio-level extension (None: nothing): the level element puts the level
    of each buffer on it (audio-level-meta, GStreamer 1.20), rtphdrextclientaudiolevel writes it in the RTP header."""
    if GstRtp is None:
        return 'the GstRtp introspection data (gir1.2-gst-plugins-base-1.0)'
    if not _has('level'):
        return 'the level element (gstreamer1.0-plugins-good)'
    if not _has('rtphdrextclientaudiolevel'):
        return 'the rtphdrextclientaudiolevel element (gstreamer1.0-plugins-good 1.20 or later)'
    level = Gst.ElementFactory.make('level', None)
    if level is None or level.find_property('audio-level-meta') is None:
        return 'the audio-level-meta property of level (GStreamer 1.20 or later)'
    return None


def _audio_level_supported():
    global _audio_level_reported
    missing = _audio_level_missing()
    if missing is not None and not _audio_level_reported:
        _audio_level_reported = True
        log.warning(f'our audio does not carry its level (ssrc-audio-level, RFC 6464): no {missing}; the room cannot show when we talk')
    return missing is None


def _audio_level_meter():
    """The element that measures our microphone for ssrc-audio-level ([] when not supported)."""
    if not _audio_level_supported():
        return []
    return [_make('level', 'audiolevelmeta', audio_level_meta=True, post_messages=False)]


def _add_audio_level_extension(payloader, caps):
    """Make payloader write ssrc-audio-level, and caps (those webrtcbin offers) name it (a=extmap). The caps,
    with the extension or without it."""
    if not _audio_level_supported():
        return caps
    extension = GstRtp.RTPHeaderExtension.create_from_uri(SSRC_AUDIO_LEVEL_URI)
    if extension is None:
        return caps
    extension.set_id(SSRC_AUDIO_LEVEL_ID)
    payloader.emit('add-extension', extension)
    caps = caps.copy()
    caps.get_structure(0).set_value('extmap-%d' % SSRC_AUDIO_LEVEL_ID, SSRC_AUDIO_LEVEL_URI)
    return caps


# What we send: the microphone, or (playback) the microphone and music mixed

AUDIO_FILES = ('.mp3', '.flac', '.wav', '.ogg', '.oga', '.opus', '.m4a', '.aac', '.aif', '.aiff', '.wma')
GAIN_FLOOR = -60.0          # dB: a strip at it or under it is off
GAIN_TOP = 12.0


def playlist(paths):
    """The audio files of paths: the files given, and those of the folders given (and their folders), in order."""
    found = []
    for path in paths or []:
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            for root, folders, files in os.walk(path):
                folders.sort()
                found += [os.path.join(root, name) for name in sorted(files) if name.lower().endswith(AUDIO_FILES)]
        elif os.path.isfile(path):
            found.append(path)
    return found


def _send_audio(media, echo_cancellation, muted):
    """The start of what media sends, up to what encodes it: our microphone (through the echo canceller, a volume for
    mute and gain, made stereo for a stereo call or room) alone, or mixed with music when media.playback names audio
    files (media.send_mixer). Linked, then _attach_send_audio(media)."""
    convert = _make('audioconvert', 'micconvert')
    microphone = [RoomMedia._audio_element(media, media.audio_input, 'microphone', sink=False)]
    if media.stereo:
        _upmix_from_source(convert)
        microphone.append(_make('capsfilter', 'micchannels', caps=_source_caps(media.audio_input)))
    microphone += [convert, _make('audioresample')]
    if echo_cancellation:
        microphone += [_make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,rate=48000,channels=1')), _make('webrtcdsp', 'dsp', probe='echoprobe')]
    media._volume = _make('volume', 'micvolume', mute=muted)
    media._volume.set_property('volume', _gain_value(getattr(media, 'mic_gain', 0.0)))
    microphone += [media._volume, _make('audioconvert'), _make('audioresample')] + (_stereo_microphone() if media.stereo else [])
    files = playlist(getattr(media, 'playback', None))
    if getattr(media, 'playback', None) and not files:
        log.warning(f'--playback: no audio files in {", ".join(media.playback)}: the microphone alone')
    if not files:
        media.send_mixer = None
        return microphone + _level('miclevel') + _monitor_tee(media, 'mic')
    media.send_mixer = SendMixer(media, files)
    return media.send_mixer.head(microphone)


def _attach_send_audio(media):
    if getattr(media, 'send_mixer', None) is not None:
        media.send_mixer.attach()
    for key, tee in list(getattr(media, '_monitor_tees', {}).items()):
        _attach_monitor(media, key, tee)


# What we hear of ourselves (monitor, --monitor or --playback): our strips (the microphone, the music) mixed with what
# we hear, right before the speakers, each on or off (off at first). For headphones: from speakers the microphone
# would take it in again.

MONITOR_CAPS = 'audio/x-raw,rate=48000,channels=2'


def _monitor_head(media):
    """The elements before the speakers that mix what we hear of ourselves in: [] without monitoring."""
    media._monitor_mixer, media._monitor_tees, media._monitors = None, {}, {}
    media.__dict__.setdefault('monitor_on', {})
    if not getattr(media, 'monitor', False) or media.audio_output == 'none':
        return []
    media._monitor_mixer = _make('audiomixer', 'monitormixer', latency=40 * Gst.MSECOND, ignore_inactive_pads=True)
    return [_make('audioconvert'), _make('audioresample'), _make('capsfilter', caps=Gst.Caps.from_string(MONITOR_CAPS)),
            media._monitor_mixer]


def _monitor_tee(media, key):
    """A tee for what we hear of strip key ([] without monitoring); linked by _attach_monitor."""
    if getattr(media, '_monitor_mixer', None) is None:
        return []
    tee = _make('tee', 'monitortee-' + key, allow_not_linked=True)
    media._monitor_tees[key] = tee
    return [tee]


def _attach_monitor(media, key, tee):
    # its queue's size is its most latency: more than what is heard of the room brings (its jitter buffer, the room's
    # mixer: 210 ms and up), else the monitor mixer cannot configure its latency and stops (leaky: never held back)
    branch = [_make('queue', max_size_time=3 * Gst.SECOND, max_size_buffers=0, max_size_bytes=0, leaky='downstream'),
              _make('audioconvert'), _make('audioresample'), _make('capsfilter', caps=Gst.Caps.from_string(MONITOR_CAPS)),
              _make('volume', 'monitor-' + key, mute=not media.monitor_on.get(key, False))]
    _link_chain(media.pipeline, branch)
    _link_into(tee, branch[0])
    _link_into(branch[-1], media._monitor_mixer)
    media._monitors[key] = branch[-1]


class SendMixer(object):
    """What we send with playback: our microphone and music mixed, each a strip with its gain in dB (strips: mic,
    music, and total for all of it; GAIN_FLOOR and under: off), for the console mixer. The music: the files given, one after the other, then
    again from the first.

        microphone -> ... -> volume (gain, mute) -> level ----------------------------\
        music -> uridecodebin -> queue -> convert/resample -> volume (gain) -> level ---> audiomixer -> level (sent)
    """

    def __init__(self, media, files):
        self.media = media
        self.files = files
        self.track = -1
        self.title = None           # the file played
        self.strips = [dict(name='mic', gain=getattr(media, 'mic_gain', 0.0), level='level-strip-mic'),
                       dict(name='music', gain=getattr(media, 'playback_gain', None) if getattr(media, 'playback_gain', None) is not None else -6.0,
                            level='level-strip-music'),
                       dict(name='total', gain=0.0, level=None)]        # all we send (its level: what is sent, miclevel)
        self.pipeline = None
        self._decoder = None
        self.paused = False
        self._block = None          # the probe that holds the music while paused
        self._paused_at = 0

    def _running(self):
        """The pipeline's running time now (ns), 0 before it plays."""
        pipeline = self.pipeline
        clock = pipeline.get_clock() if pipeline is not None else None
        if clock is None or pipeline.get_state(0)[1] != Gst.State.PLAYING:
            return 0
        return max(0, clock.get_time() - pipeline.get_base_time())

    def toggle_pause(self):
        """p: the music held where it is, or on again from there (its time moved on by the pause, else the mixer
        would take what was held as late): whether it is paused now."""
        if self.pipeline is None:
            return self.paused
        held = self._music[0].get_static_pad('src')        # the queue's: the decoder fills it, then waits
        if not self.paused:
            self._paused_at = self._running()
            self._block = held.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, lambda pad, info: Gst.PadProbeReturn.OK)
            self.paused = True
            log.info(f'playback: {self.title} paused')
        else:
            after = self._music[1].get_static_pad('sink')
            after.set_offset(after.get_offset() + self._running() - self._paused_at)
            if self._block is not None:
                held.remove_probe(self._block)
            self._block, self.paused = None, False
            log.info(f'playback: {self.title} on again')
        return self.paused

    def head(self, microphone):
        """The elements from the mixer on (to what encodes), the microphone's kept to be linked into it."""
        channels = 2 if self.media.stereo else 1
        self._caps = Gst.Caps.from_string(f'audio/x-raw,rate=48000,channels={channels}')
        self._microphone = microphone + [_make('capsfilter', caps=self._caps)] + _level('level-strip-mic') + _monitor_tee(self.media, 'mic')
        self._music_volume = _make('volume', 'musicgain')
        self._music = [_make('queue', 'musicqueue', max_size_time=2 * Gst.SECOND), _make('audioconvert'), _make('audioresample'),
                       _make('capsfilter', caps=self._caps), self._music_volume] + _level('level-strip-music') + _monitor_tee(self.media, 'music')
        self.mixer = _make('audiomixer', 'sendmixer')
        self._master = _make('volume', 'mastergain')
        return [self.mixer, _make('audioconvert'), _make('capsfilter', caps=self._caps), self._master] + _level('miclevel')

    def attach(self):
        """Once the mixer is in the pipeline: the microphone and the music linked into it, the first file played."""
        self.pipeline = pipeline = self.media.pipeline
        for chain in (self._microphone, self._music):
            _link_chain(pipeline, chain)
            _link_into(chain[-1], self.mixer)
        # where the music comes in: a file's end goes no further (the queue would stop at it), the next file instead
        self._music[0].get_static_pad('sink').add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_music_event)
        for index, strip in enumerate(self.strips):
            self.set_gain(index, strip['gain'])
        # at a random file with playback_random (then on in order), else at the first
        self._play(random.randrange(len(self.files)) if getattr(self.media, 'playback_random', False) else 0)

    def _element(self, index):
        return {'mic': self.media._volume, 'music': self._music_volume, 'total': self._master}[self.strips[index]['name']]

    def set_gain(self, index, gain):
        """A strip's gain, in dB (GAIN_FLOOR to GAIN_TOP; GAIN_FLOOR: off)."""
        gain = min(GAIN_TOP, max(GAIN_FLOOR, gain))
        self.strips[index]['gain'] = gain
        element = self._element(index)
        if element is not None:
            element.set_property('volume', 0.0 if gain <= GAIN_FLOOR else 10 ** (gain / 20))

    def levels(self):
        """{(strip name, 'strip'): level} since the last call (of each strip, after its gain)."""
        found = {}
        for strip in self.strips:
            if strip['level'] is None:
                continue        # the total: what is sent, in media.levels() already
            level = self.media._levels.take(strip['level'])
            if level is not None:
                found[(strip['name'], 'strip')] = level
        return found

    def _play(self, index):
        pipeline = self.pipeline
        if pipeline is None or self.media.pipeline is not pipeline:
            return
        old, self._decoder = self._decoder, None
        if old is not None:
            old.set_state(Gst.State.NULL)
            pipeline.remove(old)
        if self.paused:
            self.toggle_pause()         # another file: played (from its start, now)
        self._music[1].get_static_pad('sink').set_offset(0)        # no pause in it yet
        self.track = index % len(self.files)
        path = self.files[self.track]
        self.title = os.path.splitext(os.path.basename(path))[0]
        decoder = _make('uridecodebin', 'musicdecoder', uri=Gst.filename_to_uri(path))
        decoder.connect('pad-added', self._on_decoded)
        # the file's time starts now (in the pipeline's running time): what was mixed goes on
        clock = pipeline.get_clock()
        running = clock.get_time() - pipeline.get_base_time() if clock is not None and pipeline.get_state(0)[1] == Gst.State.PLAYING else 0
        self._music[0].get_static_pad('sink').set_offset(max(0, running))
        self._decoder = decoder
        pipeline.add(decoder)
        decoder.sync_state_with_parent()
        log.info(f'playback: {self.title} ({self.track + 1} of {len(self.files)})')

    def _on_decoded(self, decoder, pad):
        # streaming thread
        caps = pad.get_current_caps() or pad.query_caps(None)
        sink = self._music[0].get_static_pad('sink')
        if caps is not None and caps.get_size() and caps.get_structure(0).get_name().startswith('audio/') and not sink.is_linked():
            pad.link(sink)
        else:
            discard = _make('fakesink', sync=False)
            discard.set_property('async', False)
            self.pipeline.add(discard)
            discard.sync_state_with_parent()
            pad.link(discard.get_static_pad('sink'))

    def _on_music_event(self, pad, info):
        # streaming thread: at the end of a file, not the end of the music, the next one instead
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.EOS:
            from twisted.internet import reactor
            reactor.callFromThread(self.next)
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def next(self):
        """The next file (after the last one, the first)."""
        self._play(self.track + 1)

    def previous(self):
        self._play(self.track - 1)


MONO_TO_STEREO = '<<(float)1.0>, <(float)1.0>>'                                 # one channel in, in both out
STEREO_TO_STEREO = '<<(float)1.0, (float)0.0>, <(float)0.0, (float)1.0>>'      # as it is


def _stereo_microphone():
    """What makes our microphone two channels (after _upmix_from_source on its first audioconvert)."""
    return [_stereo_caps()]


def _device_channels(description):
    """How many channels the input device of description has (auto: the system's default one), None if not known."""
    device = description
    if not isinstance(device, AudioDevice):
        if description not in (None, '', 'auto'):
            return None         # a GStreamer description: as it gives it
        inputs, outputs = audio_devices()
        device = next((each for each in inputs if each.default), inputs[0] if inputs else None)
        if device is None:
            return None
    try:
        caps = device.device.get_caps()
        found = []
        for index in range(caps.get_size() if caps is not None else 0):
            value = caps.get_structure(index).get_value('channels') if caps.get_structure(index).has_field('channels') else None
            if isinstance(value, int):
                found.append(value)
        return max(found) if found else None
    except Exception:
        return None


def _source_caps(description):
    """The caps our microphone may give, in stereo: its own channel count when it is a mono device (one channel,
    not two of which macOS fills the left only, as it does when two are asked of a mono device)."""
    channels = _device_channels(description)
    if channels == 1:
        log.info(f'our microphone ({description if isinstance(description, AudioDevice) else "the system default"}) is mono: sent in both channels')
        return Gst.Caps.from_string('audio/x-raw,channels=1')
    log.info(f'our microphone has {channels if channels is not None else "an unknown number of"} channels: sent as it gives them')
    return Gst.Caps.from_string('audio/x-raw')


def _upmix_from_source(convert):
    """Our microphone in stereo: a mono one in both channels, equal, at full level; a stereo one as it is. Set on the
    audioconvert right after the microphone (its mix-matrix, from the caps the microphone gives), where the channels
    become two: a mono microphone that says its one channel is the front left one (macOS does) would else go to the
    left only (the first audioconvert makes it stereo by position, before anything after it could)."""

    def on_event(pad, info):
        # the microphone's thread, before the audioconvert takes the caps
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.CAPS:
            channels = _channels(event.parse_caps())
            if channels in (1, 2):
                Gst.util_set_object_arg(convert, 'mix-matrix', MONO_TO_STEREO if channels == 1 else STEREO_TO_STEREO)
                log.info('our microphone is mono: sent in both channels' if channels == 1 else 'our microphone is stereo: sent as it is')
        return Gst.PadProbeReturn.OK
    convert.get_static_pad('sink').add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_event)
    return convert


def _stereo_caps():
    """Two channels, after an audioconvert: a mono microphone goes in both, equal (audioconvert copies mono to left
    and right at full level), a stereo source stays as it is."""
    return _make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,channels=2,channel-mask=(bitmask)0x3'))


def _link_into(element, other):
    """element's src pad (requested, for a tee) to a requested sink pad of other (a mixer), or to other's sink pad;
    RuntimeError with why not."""
    source = element.get_static_pad('src') or element.request_pad_simple('src_%u')
    sink = other.get_static_pad('sink') or other.request_pad_simple('sink_%u')
    if source is None or sink is None:
        raise RuntimeError(f'cannot link {element.get_name()} to {other.get_name()}: no pad (' +
                           ('none from ' + element.get_name() if source is None else 'none from ' + other.get_name()) + ')')
    result = source.link(sink)
    if result != Gst.PadLinkReturn.OK:
        parents = f'{getattr(element.get_parent(), "get_name", lambda: None)()}, {getattr(other.get_parent(), "get_name", lambda: None)()}'
        raise RuntimeError(f'cannot link {element.get_name()} to {other.get_name()}: {result.value_nick} (in {parents})')


def _link_chain(pipeline, elements, add=True):
    if add:
        for element in elements:
            if not pipeline.add(element):
                raise RuntimeError(f'cannot add {element.get_name()} to the pipeline (a name taken?)')
    for left, right in zip(elements, elements[1:]):
        if left.get_static_pad('src') is None:         # a tee: a pad of its own for each output
            _link_into(left, right)
            continue
        if not left.link(right):
            raise RuntimeError(f'cannot link {left.get_name()} to {right.get_name()}')


def _sdp_message(text):
    result, message = GstSdp.SDPMessage.new_from_text(text)
    if result != GstSdp.SDPResult.OK:
        raise ValueError('unreadable SDP')
    return message


def _description(kind, text):
    return GstWebRTC.WebRTCSessionDescription.new(kind, _sdp_message(text))


def _watch_states(webrtcbin, label):
    """Log how the ICE and DTLS connection of a webrtcbin gets on (each change, info; gathering, debug)."""
    def changed(element, spec, what, level):
        value = element.get_property(spec.name)
        log.log(level, f'{label}: {what} {getattr(value, "value_nick", value)}')
    webrtcbin.connect('notify::ice-gathering-state', changed, 'ICE gathering', logging.DEBUG)
    webrtcbin.connect('notify::ice-connection-state', changed, 'ICE', logging.INFO)
    webrtcbin.connect('notify::connection-state', changed, 'connection (ICE+DTLS)', logging.INFO)


def _checked(label, what):
    """A promise that logs an error webrtcbin replies with (set-remote-description and the like do not raise)."""
    def done(promise, data):
        promise.wait()
        reply = promise.get_reply()
        if reply is not None and reply.has_field('error'):
            error = reply.get_value('error')
            log.error(f'{label}: {what} failed: {getattr(error, "message", error)}')
    return Gst.Promise.new_with_change_func(done, None)


def connection_state(webrtcbin):
    """'ICE x, connection y' of a webrtcbin, or ''."""
    if webrtcbin is None:
        return ''
    ice = webrtcbin.get_property('ice-connection-state')
    connection = webrtcbin.get_property('connection-state')
    return f'ICE {getattr(ice, "value_nick", ice)}, connection {getattr(connection, "value_nick", connection)}'


def _receiving(webrtcbin, media):
    """A webrtcbin that receives: its jitter buffer as long as media.jitter_buffer (ms, else webrtcbin's 200), and
    its jitter buffers kept, for the packets they drop as late (see webrtcbin_stats): those come, but after their
    time; they are no loss (no sequence number is missing), they are heard as gaps."""
    latency = getattr(media, 'jitter_buffer', None)
    if latency:
        webrtcbin.set_property('latency', int(latency))
    webrtcbin._jitterbuffers = []
    rtpbin = webrtcbin.get_by_name('rtpbin')
    if rtpbin is not None:
        rtpbin.connect('new-jitterbuffer', lambda bin, jitterbuffer, session, ssrc: webrtcbin._jitterbuffers.append(jitterbuffer))


def _configure_ice(webrtcbin, ice_servers):
    stun, turn = ice_urls(ice_servers)
    if stun:
        webrtcbin.set_property('stun-server', stun)
    for url in turn:
        webrtcbin.emit('add-turn-server', url)


def _video_preferences(codecs, ssrc):
    preferences = Gst.Caps.new_empty()
    for name, pt in codecs:
        caps = (f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000,rtcp-fb-nack=(boolean)true,'
                f'rtcp-fb-nack-pli=(boolean)true,rtcp-fb-ccm-fir=(boolean)true,ssrc=(uint){ssrc}')
        if name == 'H264':
            caps += ',' + H264_FMTP
        preferences.append(Gst.Caps.from_string(caps))
    return preferences


def _encoder(factory, bitrate, fps):
    if factory == 'vp9enc':
        return _make('vp9enc', deadline=1, cpu_used=8, row_mt=True, target_bitrate=bitrate * 1000, keyframe_max_dist=fps * 2,
                     end_usage='cbr', lag_in_frames=0, error_resilient='default')
    if factory == 'vp8enc':
        return _make('vp8enc', deadline=1, cpu_used=8, target_bitrate=bitrate * 1000, keyframe_max_dist=fps * 2,
                     end_usage='cbr', lag_in_frames=0, error_resilient='default')
    if factory == 'x264enc':
        return _make('x264enc', tune='zerolatency', speed_preset='ultrafast', bitrate=bitrate, key_int_max=fps * 2, bframes=0)
    return _make('openh264enc', bitrate=bitrate * 1000, gop_size=fps * 2)


def _encodable_codecs():
    found = []
    for name, pt, encoder, payloader in VIDEO_CODECS:
        if (name, pt) not in found and _has(encoder) and _has(payloader):
            found.append((name, pt))
    return found


def _encoder_for(name):
    return next(((encoder, payloader) for codec, pt, encoder, payloader in VIDEO_CODECS if codec == name and _has(encoder) and _has(payloader)), None)


def _mid(webrtcbin, mline):
    description = webrtcbin.get_property('local-description')
    found = mids(description.sdp.as_text()) if description is not None else []
    return found[mline] if mline < len(found) else None


def _link_camera(pipeline, video_pad, source, name, pt, ssrc, default_fps, label):
    """A local camera (dict(kind='v4l2', device, resolution, fps) or dict(kind='test', pattern, ...)), encoded with
    the codec the answer chose, to a webrtcbin sink pad: source -> decodebin -> convert/scale/rate -> encoder -> payloader."""
    encoders = _encoder_for(name)
    if encoders is None:
        raise RuntimeError(f'no encoder for {name}')
    encoder_factory, payloader_factory = encoders
    width, height, bitrate = parse_resolution(source.get('resolution') or '640x360')
    fps = int(source.get('fps') or default_fps)
    video_pad.get_property('transceiver').set_property('codec-preferences', Gst.Caps.from_string(
        f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000,ssrc=(uint){ssrc}'))
    if source['kind'] == 'v4l2':
        element = _make('v4l2src', 'camera', device=source.get('device') or '/dev/video0')
    else:
        element = _make('videotestsrc', 'camera', is_live=True, pattern=source.get('pattern') or 'smpte')
    decodebin = _make('decodebin')
    chain = [_make('videoconvert'), _make('videoscale', add_borders=True), _make('videorate', drop_only=True),
             _make('capsfilter', caps=Gst.Caps.from_string(f'video/x-raw,format=I420,width={width},height={height},framerate={fps}/1,pixel-aspect-ratio=1/1')),
             _make('queue', max_size_buffers=2, leaky='downstream'), _encoder(encoder_factory, bitrate, fps)]
    if name == 'H264':
        chain.append(_make('capsfilter', caps=Gst.Caps.from_string('video/x-h264,profile=constrained-baseline')))
    chain += [_make(payloader_factory, pt=pt, mtu=1200, config_interval=-1, ssrc=ssrc),
              _make('capsfilter', caps=Gst.Caps.from_string(f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000'))]
    pipeline.add(element)
    pipeline.add(decodebin)
    _link_chain(pipeline, chain)
    if chain[-1].get_static_pad('src').link(video_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError('cannot link our video to webrtcbin')
    element.link(decodebin)
    decodebin.connect('pad-added', lambda bin, pad: ExtraMedia._link_video(pad, chain[0]))
    for item in [element, decodebin] + chain:
        item.sync_state_with_parent()
    log.info(f'{label}: sending our camera {source.get("device") or "test pattern " + (source.get("pattern") or "smpte")} '
             f'as {name} {width}x{height} {fps} fps, {bitrate} kbit/s ({encoder_factory})')


class MediaFactory(object):
    """media_factory for VideoroomSession.

    audio_input: 'auto' (the default microphone), 'none' (silence) or a GStreamer source description.
    audio_output: 'auto' (the default speakers), 'none' or a GStreamer sink description.
    video_sink: None (the feeds' video is not decoded), or a callable(participant) that returns a GStreamer
    sink element for that participant's decoded video.
    stereo: stereo audio (sent and played), for a stereo room (a new one becomes stereo): Opus in stereo, no echo
    canceller.
    """

    def __init__(self, audio_input='auto', audio_output='auto', video_sink=None, video_source=None, record_tracks=None,
                 track_paths=None, stereo=False, playback=None, monitor=False, playback_gain=None, mic_gain=0.0, jitter_buffer=None,
                 playback_random=False):
        self.playback = playback                # audio files (or folders) mixed into what we send, see SendMixer
        self.monitor = bool(monitor or playback)    # what we send, heard here too (see _monitor_head)
        self.monitor_on = {'mic': True, 'music': True} if monitor else {}     # --monitor: heard from the start
        self.playback_gain = playback_gain          # the music's gain at first (dB), else SendMixer's default
        self.mic_gain = mic_gain                    # our input device's gain at first (dB)
        self.jitter_buffer = jitter_buffer          # ms of the jitter buffer (webrtcbin's latency), None: 200
        self.playback_random = playback_random      # the music from a random file of it, not the first
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.video_sink = video_sink
        self.video_source = video_source
        self.stereo = stereo
        self.record_tracks = record_tracks      # a folder: each participant's audio into a file of its own there
        self.track_paths = track_paths          # a list the files' paths are added to, as they are made

    def __call__(self, session):
        media = RoomMedia(session, self.audio_input, self.audio_output, self.video_sink, self.video_source, self.stereo)
        media.record_tracks, media.track_paths = self.record_tracks, self.track_paths
        media.playback, media.send_mixer, media.monitor = self.playback, None, self.monitor
        media.monitor_on = dict(self.monitor_on)
        media.playback_gain = self.playback_gain
        media.mic_gain = self.mic_gain
        media.jitter_buffer = self.jitter_buffer
        media.playback_random = self.playback_random
        return media


def _gain_value(gain):
    """A volume element's volume for a gain in dB (GAIN_FLOOR: 0, off)."""
    return 0.0 if gain <= GAIN_FLOOR else 10 ** (gain / 20)


class _Strips(object):
    """The console mixer of a media (RoomMedia, CallMedia): what we send (the microphone; with playback the music and
    the total too, see SendMixer), then what we hear (the far end, or each participant: a volume each before the
    speakers), each a strip with a gain in dB. The media gives _listen(): [(key, name)] of what we hear."""

    mic_gain = 0.0

    def _listen(self):
        return []

    def mixer_strips(self):
        """[dict(kind ('send' or 'listen'), key, name, gain)], in the order of the panel."""
        if getattr(self, 'send_mixer', None) is not None:
            strips = [dict(kind='send', key=strip['name'], name=strip['name'], gain=strip['gain']) for strip in self.send_mixer.strips]
        else:
            strips = [dict(kind='send', key='mic', name='mic', gain=self.mic_gain)]
        monitors, on = getattr(self, '_monitors', {}), self.__dict__.setdefault('monitor_on', {})
        for strip in strips:
            strip['monitor'] = on.get(strip['key'], False) if strip['key'] in monitors else None     # None: not to be heard here
        gains = self.__dict__.setdefault('listen_gains', {})
        return strips + [dict(kind='listen', key=key, name=name, gain=gains.get(key, 0.0), monitor=None) for key, name in self._listen()]

    def set_strip_monitor(self, key, on):
        """Whether we hear strip key (mic, music) here too: False when it cannot be (no monitoring)."""
        volume = getattr(self, '_monitors', {}).get(key)
        if volume is None:
            return False
        self.__dict__.setdefault('monitor_on', {})[key] = bool(on)
        volume.set_property('mute', not on)
        return True

    def set_strip_gain(self, kind, key, gain):
        gain = min(GAIN_TOP, max(GAIN_FLOOR, gain))
        if kind == 'send':
            mixer = getattr(self, 'send_mixer', None)
            if mixer is not None:
                names = [strip['name'] for strip in mixer.strips]
                if key in names:
                    mixer.set_gain(names.index(key), gain)
            elif key == 'mic':
                self.mic_gain = gain
                if getattr(self, '_volume', None) is not None:
                    self._volume.set_property('volume', _gain_value(gain))
        else:
            self.__dict__.setdefault('listen_gains', {})[key] = gain       # kept for a feed made later, too
            volume = self.__dict__.setdefault('_listen_volumes', {}).get(key)
            if volume is not None:
                volume.set_property('volume', _gain_value(gain))

    def _listen_volume(self, key, name):
        """The volume element of what we hear from key, at its gain."""
        volume = _make('volume', name)
        volume.set_property('volume', _gain_value(self.__dict__.setdefault('listen_gains', {}).get(key, 0.0)))
        self.__dict__.setdefault('_listen_volumes', {})[key] = volume
        return volume


class RoomMedia(_Strips):
    """video_source: our own video (session.video), as an extra video's source: dict(kind='v4l2', device, resolution, fps)
    or dict(kind='test', pattern, resolution, fps); the extra videos are shown under it."""

    MAIN_FPS = 15

    def __init__(self, session, audio_input='auto', audio_output='auto', video_sink=None, video_source=None, stereo=False):
        self.session = session
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.video_sink = video_sink
        self.video_source = video_source
        self.stereo = stereo
        self.pipeline = None
        self.publisher = None
        self._mixer = None
        self._volume = None
        self._video_pad = None
        self._video_ssrc = random.randint(1, 0xffffffff)
        self._levels = _Levels()
        self.record_tracks = None       # a folder (MediaFactory): a FLAC per participant, see _record_track
        self.track_paths = None
        self._tracks_origin = None      # when the tracks start (time.monotonic(): the media started)
        self._tracks = []               # their branches: [(first element, [elements])], ended at finish_tracks

    # The publisher

    def start(self):
        missing = media_missing()
        if missing:
            raise RuntimeError('missing ' + ', '.join(f'{what} ({package})' for what, package in missing))
        if self.session.video and (self.video_source or {}).get('kind') not in ('v4l2', 'test'):
            raise RuntimeError('our own video must be a local camera (v4l2 or test)')
        self.pipeline = Gst.Pipeline.new('videoroom')
        self.pipeline.use_clock(Gst.SystemClock.obtain())     # the microphone or speakers may change while playing
        self._tracks_origin = time.monotonic()
        self.pipeline.get_bus().set_sync_handler(self._on_bus_message, None)
        echo_cancellation = not self.stereo and self.audio_input != 'none' and self.audio_output != 'none' and _has('webrtcdsp') and _has('webrtcechoprobe')

        # what is played: every feed's audio, mixed (in stereo for a stereo room)
        self._mixer = _make('audiomixer', 'mixer', ignore_inactive_pads=True)
        playback = [self._mixer] + ([_stereo_caps()] if self.stereo else []) + [_make('audioconvert'), _make('audioresample')]
        if echo_cancellation:
            playback.append(_make('webrtcechoprobe', 'echoprobe'))
        playback += _monitor_head(self)
        playback.append(self._audio_element(self.audio_output, 'speakers', sink=True))
        _link_chain(self.pipeline, playback)

        self.publisher = webrtcbin = _make('webrtcbin', 'publisher', bundle_policy='max-bundle')
        _configure_ice(webrtcbin, self.session.ice_servers)
        self.pipeline.add(webrtcbin)
        self._watch_webrtcbin(webrtcbin, self.session.id)

        microphone = _send_audio(self, echo_cancellation, self.session.audio_muted)
        payloader = _make('rtpopuspay', pt=OPUS_PT)
        microphone += _audio_level_meter() + [
                       _make('queue', max_size_time=200 * Gst.MSECOND, leaky='downstream'),
                       _opus_encoder(self.stereo),
                       payloader]
        audio_caps = _add_audio_level_extension(payloader, _opus_caps(OPUS_PT, self.stereo))
        if audio_caps.get_structure(0).has_field('extmap-%d' % SSRC_AUDIO_LEVEL_ID):
            log.info('our audio carries its level (ssrc-audio-level, RFC 6464): the room shows when we talk')
        microphone.append(_make('capsfilter', caps=audio_caps))
        _link_chain(self.pipeline, microphone)
        _attach_send_audio(self)

        def first(pad, info):
            log.info(f'{self.session.room}: our first audio packet to the room')
            return Gst.PadProbeReturn.REMOVE
        microphone[-1].get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, first)
        audio_pad = webrtcbin.request_pad_simple('sink_%u')
        transceiver = audio_pad.get_property('transceiver')
        # listening only (session.listen_only: a recorder): offered inactive, nothing of ours sent
        transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.INACTIVE
                                 if getattr(self.session, 'listen_only', False) else GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        transceiver.set_property('codec-preferences', audio_caps)     # the offer is made before the microphone has caps
        if microphone[-1].get_static_pad('src').link(audio_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError('cannot link the microphone to webrtcbin')

        if self.session.video:
            # our camera: offered with every codec we can encode and our SSRC, linked once the answer names the codec
            codecs = _encodable_codecs()
            if not codecs:
                raise RuntimeError('no video encoder (vp9enc, vp8enc, x264enc or openh264enc)')
            self._video_pad = webrtcbin.request_pad_simple('sink_%u')
            video_transceiver = self._video_pad.get_property('transceiver')
            video_transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
            video_transceiver.set_property('codec-preferences', _video_preferences(codecs, self._video_ssrc))

        self.pipeline.set_state(Gst.State.PLAYING)
        webrtcbin.emit('create-offer', None, Gst.Promise.new_with_change_func(self._on_offer_created, None))

    def _audio_element(self, description, name, sink):
        if description == 'none':
            if sink:
                element = _make('fakesink', name, sync=False)
                element.set_property('async', False)
                return element
            return _make('audiotestsrc', name, wave='silence', is_live=True)
        if description in (None, '', 'auto'):
            return _make('autoaudiosink' if sink else 'autoaudiosrc', name)
        if isinstance(description, AudioDevice):
            return description.element(name)
        element = Gst.parse_bin_from_description(description, True)
        element.set_name(name)
        return element

    def _on_offer_created(self, promise, data):
        # streaming thread
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        if offer is None:
            self.session.media_error('publisher', 'cannot create the offer', True)
            return
        self.publisher.emit('set-local-description', offer, Gst.Promise.new())
        text = offer.sdp.as_text()
        if self.stereo:
            text = with_opus_stereo(text)      # what makes the room stereo (whatever this GStreamer puts in the fmtp)
        self.session.publisher_offer(text)

    def set_publisher_answer(self, sdp):
        if self._video_pad is not None:
            codec = chosen_video_codec(sdp)
            if codec is None:
                log.warning(f'{self.session.room}: the room takes no video from us')
            else:
                self._link_camera(*codec)
        self.publisher.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), _checked(self.session.room, 'using the answer'))

    def _link_camera(self, name, pt):
        _link_camera(self.pipeline, self._video_pad, self.video_source, name, pt, self._video_ssrc, self.MAIN_FPS, self.session.room)

    def set_audio_muted(self, muted):
        if self._volume is not None:
            self._volume.set_property('mute', bool(muted))

    def set_audio_device(self, kind, description):
        """Other speakers (kind output) or another microphone (input): auto, none, a GStreamer description or an
        AudioDevice; replaced in the running pipeline."""
        _replace_audio(self, kind, description)

    def stop(self):
        if self.pipeline is not None:
            pipeline, self.pipeline = self.pipeline, None
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_bus().set_sync_handler(None, None)
        self.publisher = None

    # ICE and the bus

    def _watch_webrtcbin(self, webrtcbin, label):
        session = self.session
        _watch_states(webrtcbin, f'{self.session.room} {label}')
        webrtcbin.connect('on-negotiation-needed', lambda element: None)
        webrtcbin.connect('on-ice-candidate', lambda element, mline, candidate: session.candidate(label, mline, _mid(element, mline), candidate))
        webrtcbin.connect('notify::ice-gathering-state', lambda element, spec: element.get_property('ice-gathering-state') == GstWebRTC.WebRTCICEGatheringState.COMPLETE and session.end_of_candidates(label))
        webrtcbin.connect('notify::ice-connection-state', lambda element, spec: element.get_property('ice-connection-state') == GstWebRTC.WebRTCICEConnectionState.FAILED and session.ice_failed(label))

    def _on_bus_message(self, bus, message, data):
        # any thread
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source = message.src.get_name() if message.src is not None else '?'
            fatal = source.startswith(('publisher', 'microphone', 'speakers', 'mixer'))     # our camera ('camera') failing is not
            log.debug(f'{self.session.room}: GStreamer error in {source}: {error.message} ({debug})')
            self.session.media_error(source, error.message, fatal)
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            log.debug(f'{self.session.room}: GStreamer warning in {message.src.get_name() if message.src else "?"}: {warning.message}')
        elif message.type == Gst.MessageType.ELEMENT and message.src is not None:
            name = message.src.get_name()
            peak = self._levels.add(message.get_structure(), name)
            if peak is not None and name.startswith('level-'):
                self.session.level(name[6:], peak > SPEAKING_DB)
        return Gst.BusSyncReply.DROP

    # Feeds

    def add_feed(self, participant, sdp):
        webrtcbin = _make('webrtcbin', 'feed-' + participant.feed, bundle_policy='max-bundle')
        _receiving(webrtcbin, self)
        _configure_ice(webrtcbin, self.session.ice_servers)
        participant.media = dict(webrtcbin=webrtcbin, elements=[webrtcbin], mixer_pads=[], cvo_id=cvo_id(sdp))
        self.pipeline.add(webrtcbin)
        webrtcbin.connect('pad-added', self._on_feed_pad, participant)
        self._watch_webrtcbin(webrtcbin, participant.feed)
        webrtcbin.sync_state_with_parent()

        def remote_set(promise, data):
            promise.wait()
            webrtcbin.emit('create-answer', None, Gst.Promise.new_with_change_func(answer_created, None))

        def answer_created(promise, data):
            promise.wait()
            reply = promise.get_reply()
            answer = reply.get_value('answer') if reply is not None else None
            if answer is None:
                self.session.media_error('feed-' + str(participant.feed), f'cannot answer {participant.uri}', False)
                return
            webrtcbin.emit('set-local-description', answer, Gst.Promise.new())
            text = answer.sdp.as_text()
            if participant.media['cvo_id'] is not None:
                text = with_cvo(text, participant.media['cvo_id'])     # webrtcbin does not answer extensions it does not implement
            self.session.feed_answer(participant, text)

        webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.OFFER, sdp), Gst.Promise.new_with_change_func(remote_set, None))

    def _add_branch(self, participant, elements, pad):
        for element in elements:
            self.pipeline.add(element)
            participant.media['elements'].append(element)
        _link_chain(self.pipeline, elements, add=False)
        for element in elements:
            element.sync_state_with_parent()
        pad.link(elements[0].get_static_pad('sink'))

    def _discard(self, participant, pad):
        sink = _make('fakesink', sync=False)
        sink.set_property('async', False)
        self._add_branch(participant, [sink], pad)

    def _on_feed_pad(self, webrtcbin, pad, participant):
        # streaming thread
        if pad.get_direction() != Gst.PadDirection.SRC or self.pipeline is None:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        media = caps.get_structure(0).get_string('media') if caps is not None and caps.get_size() else None
        encoding = caps.get_structure(0).get_string('encoding-name') if caps is not None and caps.get_size() else None
        log.info(f'{participant.name}: {media or "?"} arrives ({encoding or "?"})' + (': not decoded (not shown)' if media == 'video' and self.video_sink is None else ''))
        if media == 'video' and self.video_sink is None:
            self._discard(participant, pad)     # nobody looks at it: not even decoded
            return
        decodebin = _make('decodebin')
        decodebin.connect('pad-added', self._on_decoded_pad, participant)
        decodebin.connect('deep-element-added', self._on_feed_element_added)
        self._add_branch(participant, [decodebin], pad)

    @staticmethod
    def _on_feed_element_added(bin, sub_bin, element):
        # the depayloaders would handle the RTP header extensions themselves; after a feed's video is
        # paused and resumed one can drop a frame holding extension buffers and GStreamer aborts
        if element.find_property('auto-header-extension') is not None:
            element.set_property('auto-header-extension', False)

    def _on_decoded_pad(self, decodebin, pad, participant):
        # streaming thread
        if self.pipeline is None:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        kind = caps.get_structure(0).get_name() if caps is not None and caps.get_size() else ''
        if kind.startswith('audio/'):
            chain = [_make('queue', max_size_buffers=0, max_size_bytes=0, max_size_time=RECEIVED_AUDIO_QUEUE * Gst.SECOND, leaky='downstream'), _make('audioconvert'), _make('audioresample'),
                     _make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,rate=48000,channels=2'))]
            participant.media['channels'] = _channels(caps)     # 1: a mono participant, 2: a stereo one
            tee = _make('tee') if self.record_tracks else None
            if tee is not None:         # its own file too (record_tracks), from the tee
                chain += [tee, _make('queue')]
            chain.append(self._listen_volume(participant.name, 'listen-' + participant.feed))
            if _has('level'):
                chain.append(_make('level', 'level-' + participant.feed, interval=LEVEL_INTERVAL, post_messages=True))
            structure = caps.get_structure(0) if caps is not None and caps.get_size() else None
            rate = structure.get_value('rate') if structure is not None and structure.has_field('rate') else None
            log.info(f'{participant.name}: audio decoded ({participant.media["channels"] or "?"} channel(s), {rate or "?"} Hz): to the mixer')

            def first(pad, info):
                log.info(f'{participant.name}: first audio to the mixer')
                return Gst.PadProbeReturn.REMOVE
            chain[-1].get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, first)
            self._add_branch(participant, chain, pad)
            mixer_pad = self._mixer.request_pad_simple('sink_%u')
            chain[-1].get_static_pad('src').link(mixer_pad)
            participant.media['mixer_pads'].append(mixer_pad)
            if tee is not None:
                try:
                    self._record_track(participant, tee)
                except Exception as e:
                    log.warning(f'{self.session.room}: cannot record {participant.name} on a track of its own: {e}')
        elif kind.startswith('video/') and self.video_sink is not None:
            self._add_branch(participant, [_make('queue', max_size_buffers=2, leaky='downstream'), _make('videoconvert'), self.video_sink(participant)], pad)
        else:
            self._discard(participant, pad)

    # Each participant's own track (record_tracks): FLAC, 48 kHz stereo, starting with as much silence as the
    # participant came after the recording started, so every track starts at the same moment (laid side by side in a
    # DAW, they line up). Someone leaving and coming back: a new file ("Name 2"), also aligned.

    TRACK_CAPS = 'audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=2'

    def _track_path(self, participant):
        name = re.sub(r'[/\\:\x00]', '_', participant.name or participant.uri or 'participant').strip() or 'participant'
        path = os.path.join(self.record_tracks, name + '.flac')
        number = 2
        while os.path.exists(path) or (self.track_paths is not None and path in self.track_paths):
            path = os.path.join(self.record_tracks, f'{name} {number}.flac')
            number += 1
        return path

    def _record_track(self, participant, tee):
        os.makedirs(self.record_tracks, mode=0o700, exist_ok=True)
        path = self._track_path(participant)
        offset = max(0.0, time.monotonic() - (self._tracks_origin or time.monotonic()))
        quanta = int(round(offset * 100))       # 10 ms buffers of silence before it
        # (while its silence is written, the participant's audio waits here; never holding the tee up, so the room
        # plays on: FLAC writes silence much faster than real time, an hour's in seconds)
        live = [_make('queue', max_size_buffers=0, max_size_bytes=0, max_size_time=30 * Gst.SECOND, leaky='downstream'), _make('audioconvert'),
                _make('capsfilter', caps=Gst.Caps.from_string(self.TRACK_CAPS))]
        # one segment from there on (the silence's, then the participant's running times: the encoder aborted on them)
        tail = [_make('identity', single_segment=True), _make('audioconvert'), _make('flacenc'),
                _make('filesink', location=path, sync=False, **{'async': False})]
        elements = list(live) + tail
        if quanta:
            silence = [_make('audiotestsrc', wave='silence', is_live=False, samplesperbuffer=480, num_buffers=quanta),
                       _make('capsfilter', caps=Gst.Caps.from_string(self.TRACK_CAPS))]
            concat = _make('concat')
            elements += silence + [concat]
            for element in elements:
                self.pipeline.add(element)
            _link_chain(self.pipeline, silence, add=False)
            _link_chain(self.pipeline, live, add=False)
            silence[-1].get_static_pad('src').link(concat.request_pad_simple('sink_%u'))    # first the silence,
            live[-1].get_static_pad('src').link(concat.request_pad_simple('sink_%u'))       # then the participant
            concat.link(tail[0])
            _link_chain(self.pipeline, tail, add=False)
        else:
            for element in elements:
                self.pipeline.add(element)
            _link_chain(self.pipeline, elements, add=False)
        for element in reversed(elements):
            element.sync_state_with_parent()
        source = tee.request_pad_simple('src_%u') if hasattr(tee, 'request_pad_simple') else tee.get_request_pad('src_%u')
        source.link(live[0].get_static_pad('sink'))
        participant.media['track'] = (live[0], elements)
        self._tracks.append((live[0], elements))
        if self.track_paths is not None:
            self.track_paths.append(path)
        log.info(f'{self.session.room}: recording {participant.name} into {path}' + (f' (from {offset:.1f} s)' if quanta else ''))

    def _end_track(self, track, later=True):
        """The file closed (FLAC's header with its length): an end of stream into the track, its elements let go once
        it has been written."""
        first, elements = track
        pad = first.get_static_pad('sink')
        peer = pad.get_peer()
        if peer is not None:
            peer.unlink(pad)
            parent = peer.get_parent_element()
            if parent is not None and parent.get_factory().get_name() == 'tee':
                parent.release_request_pad(peer)
        pad.send_event(Gst.Event.new_eos())
        if track in self._tracks:
            self._tracks.remove(track)
        if not later:
            return

        def release():
            pipeline = self.pipeline
            for element in elements:
                element.set_state(Gst.State.NULL)
                if pipeline is not None and element.get_parent() is pipeline:
                    pipeline.remove(element)
        threading.Timer(1.0, release).start()

    def finish_tracks(self):
        """Leaving: every track's file closed (before the pipeline is stopped)."""
        for track in list(self._tracks):
            self._end_track(track, later=False)

    def _listen(self):
        return [(participant.name, participant.name) for participant in list(self.session.feeds.values()) if participant.media]

    def remove_feed(self, participant):
        self.__dict__.setdefault('_listen_volumes', {}).pop(participant.name, None)
        track = (participant.media or {}).get('track')
        if track is not None and self.pipeline is not None:
            self._end_track(track)          # (its elements not the feed's: they go a moment later)
        media, participant.media = participant.media, None
        if media is None or self.pipeline is None:
            return
        for element in reversed(media['elements']):
            element.set_state(Gst.State.NULL)
            self.pipeline.remove(element)
        for pad in media['mixer_pads']:
            self._mixer.release_request_pad(pad)

    # Statistics

    def stats(self):
        """[(label, direction, stats)] of every peer connection of the room: our audio, our extra videos, the feeds
        (see webrtcbin_stats)."""
        found = []
        if self.publisher is not None:
            found.append(('audio+video' if self._video_pad is not None else 'audio', 'out', webrtcbin_stats(self.publisher)))
        for extra in list(self.session.extra_videos.values()):
            if extra.state == 'established' and extra.media is not None and extra.media.webrtcbin is not None:
                found.append((extra.label, 'out', webrtcbin_stats(extra.media.webrtcbin)))
        for participant in list(self.session.feeds.values()):
            if participant.media and participant.media.get('webrtcbin') is not None:
                found.append((participant.name, 'in', webrtcbin_stats(participant.media['webrtcbin'])))
        return found

    def levels(self):
        """{(label, direction): (RMS dBFS, peak dBFS, per channel)} since the last call, labelled as in stats(): what our microphone
        sends, what every feed plays."""
        found = {}
        mic = self._levels.take('miclevel')
        if mic is not None:
            found[('audio+video' if self._video_pad is not None else 'audio', 'out')] = mic
        for participant in list(self.session.feeds.values()):
            if participant.media:
                level = _as_channels(self._levels.take('level-' + participant.feed), participant.media.get('channels'))
                if level is not None:
                    found[(participant.name, 'in')] = level
        if getattr(self, 'send_mixer', None) is not None:
            found.update(self.send_mixer.levels())
        return found

    # Extra videos

    def create_extra(self, extra):
        return ExtraMedia(extra, self.session.ice_servers)


class ExtraMedia(object):
    """The media of an extra video.

    extra.source: dict(kind='rtsp', url, user, password, transcode='auto'|'yes'|'no', resolution, fps),
    dict(kind='v4l2', device, resolution, fps) or dict(kind='test', pattern, resolution, fps).
    transcode 'auto' relays an H264 camera as it is when the room takes H264 and encodes it otherwise;
    'no' only relays (the room must be H264), 'yes' always encodes.
    """

    def __init__(self, extra, ice_servers):
        self.extra = extra
        self.source = extra.source
        self.ice_servers = ice_servers
        self.pipeline = None
        self.webrtcbin = None
        self.ssrc = random.randint(1, 0xffffffff)
        self.width, self.height, self.bitrate = parse_resolution(self.source.get('resolution'))
        self.fps = int(self.source.get('fps') or DEFAULT_FPS)
        self.transcode = str(self.source.get('transcode') or 'auto').lower()
        self.source_encoding = None     # what an RTSP camera sends (H264, H265, JPEG ...)
        self.relay = False              # sent as the camera sends it
        self.bytes_sent = 0             # RTP bytes given to webrtcbin
        self._tee = None
        self._video_pad = None
        self._offered = False

    def log(self, text, level='info'):
        self.extra.log(text, level)

    def start(self):
        missing = media_missing()
        if missing:
            raise RuntimeError('missing ' + ', '.join(f'{what} ({package})' for what, package in missing))
        kind = self.source.get('kind')
        self.pipeline = pipeline = Gst.Pipeline.new('extra-video')
        pipeline.get_bus().set_sync_handler(self._on_bus_message, None)
        self.webrtcbin = webrtcbin = _make('webrtcbin', 'extra-publisher', bundle_policy='max-bundle')
        _configure_ice(webrtcbin, self.ice_servers)
        pipeline.add(webrtcbin)
        session, label = self.extra.session, self.extra.id
        webrtcbin.connect('on-negotiation-needed', lambda element: None)
        webrtcbin.connect('on-ice-candidate', lambda element, mline, candidate: session.candidate(label, mline, _mid(element, mline), candidate))
        webrtcbin.connect('notify::ice-gathering-state', lambda element, spec: element.get_property('ice-gathering-state') == GstWebRTC.WebRTCICEGatheringState.COMPLETE and session.end_of_candidates(label))
        webrtcbin.connect('notify::ice-connection-state', lambda element, spec: element.get_property('ice-connection-state') == GstWebRTC.WebRTCICEConnectionState.FAILED and self.extra.failed('the media connection failed (ICE)'))
        self._video_pad = webrtcbin.request_pad_simple('sink_%u')
        self._video_pad.get_property('transceiver').set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        self._video_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_sent)

        # until the answer says the codec, what the source produces goes to a fakesink (an unlinked pad would stop it)
        self._tee = tee = _make('tee', 'split')
        hold = _make('fakesink', sync=False)
        hold.set_property('async', False)
        _link_chain(pipeline, [tee, _make('queue', max_size_buffers=2, leaky='downstream'), hold])

        if kind == 'rtsp':
            source = _make('rtspsrc', 'source', location=self.source['url'], latency=200, protocols='tcp')
            if self.source.get('user'):
                source.set_property('user-id', self.source['user'])
                source.set_property('user-pw', self.source.get('password') or '')
            pipeline.add(source)
            source.connect('select-stream', self._select_video_stream)
            source.connect('pad-added', self._on_rtsp_pad)
            # the offer waits for the camera: it says what it sends (relayed as it is or encoded here)
        else:
            if kind == 'v4l2':
                source = _make('v4l2src', 'source', device=self.source.get('device') or '/dev/video0')
            elif kind == 'test':
                source = _make('videotestsrc', 'source', is_live=True, pattern=self.source.get('pattern') or 'smpte')
            else:
                raise RuntimeError(f'unknown video source {kind}')
            raw = [_make('videoconvert'), _make('videoscale', add_borders=True), _make('videorate', drop_only=True),
                   _make('capsfilter', caps=self._raw_caps())]
            decodebin = _make('decodebin')
            pipeline.add(source)
            pipeline.add(decodebin)
            _link_chain(pipeline, raw)
            raw[-1].link(tee)
            source.link(decodebin)
            decodebin.connect('pad-added', lambda element, pad: self._link_video(pad, raw[0]))
        pipeline.set_state(Gst.State.PLAYING)
        if self.pipeline is None:
            return              # the source failed at once (a device that is not there): already stopped
        if kind != 'rtsp':
            self._offer()       # webrtcbin makes no offer before it runs

    def _on_sent(self, pad, info):
        # streaming thread: what goes to webrtcbin
        buffer = info.get_buffer()
        if buffer is not None:
            if not self.bytes_sent:
                log.debug(f'{self.extra.label}: first video packet to webrtcbin')
            self.bytes_sent += buffer.get_size()
        return Gst.PadProbeReturn.OK

    def _raw_caps(self):
        return Gst.Caps.from_string(f'video/x-raw,format=I420,width={self.width},height={self.height},framerate={self.fps}/1,pixel-aspect-ratio=1/1')

    @staticmethod
    def _link_video(pad, element):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is not None and caps.get_size() and caps.get_structure(0).get_name().startswith('video/') and not element.get_static_pad('sink').is_linked():
            pad.link(element.get_static_pad('sink'))

    @staticmethod
    def _select_video_stream(rtspsrc, number, caps):
        # the camera's audio and metadata are not even set up
        return caps is not None and caps.get_size() > 0 and caps.get_structure(0).get_string('media') == 'video'

    def _on_rtsp_pad(self, rtspsrc, pad):
        # streaming thread
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps is not None and caps.get_size() else None
        if structure is None or structure.get_string('media') != 'video' or self._tee.get_static_pad('sink').is_linked():
            sink = _make('fakesink', sync=False)
            sink.set_property('async', False)
            self.pipeline.add(sink)
            sink.sync_state_with_parent()
            pad.link(sink.get_static_pad('sink'))
            return
        self.source_encoding = (structure.get_string('encoding-name') or '').upper()
        pad.link(self._tee.get_static_pad('sink'))
        self._offer()

    def _offer(self):
        if self._offered or self.pipeline is None:
            return
        self._offered = True
        codecs = _encodable_codecs() if self.transcode != 'no' else []
        if self.source_encoding is not None:
            relayable = self.source_encoding == 'H264' and self.transcode != 'yes' and _has('rtph264depay') and _has('h264parse')
            if relayable:
                codecs = [('H264', 102)] + [codec for codec in codecs if codec[0] != 'H264']
            if self.transcode == 'no' and not relayable:
                self.extra.failed(f'the camera sends {self.source_encoding or "nothing usable"}, only H264 can be relayed without encoding')
                return
            self.log(f'the camera sends {self.source_encoding}')
        if not codecs:
            self.extra.failed('no video encoder (vp9enc, vp8enc, x264enc or openh264enc)')
            return
        self._video_pad.get_property('transceiver').set_property('codec-preferences', _video_preferences(codecs, self.ssrc))
        self.webrtcbin.emit('create-offer', None, Gst.Promise.new_with_change_func(self._on_offer_created, None))

    def _on_offer_created(self, promise, data):
        # streaming thread
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        if offer is None or self.webrtcbin is None:
            self.extra.failed('cannot create the offer')
            return
        self.webrtcbin.emit('set-local-description', offer, Gst.Promise.new())
        self.extra.offer(offer.sdp.as_text())

    def set_answer(self, sdp):
        codec = chosen_video_codec(sdp)
        if codec is None:
            raise RuntimeError('the room takes no video from us')
        name, pt = codec
        # the offer carried fmtp the encoder's output may not match: narrowed to the codec and our SSRC
        self._video_pad.get_property('transceiver').set_property('codec-preferences', Gst.Caps.from_string(
            f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000,ssrc=(uint){self.ssrc}'))
        rtp_caps = _make('capsfilter', caps=Gst.Caps.from_string(f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000'))
        self.relay = self.source_encoding == 'H264' and name == 'H264' and self.transcode != 'yes'
        if self.relay:
            chain = [_make('queue', max_size_time=500 * Gst.MSECOND, leaky='downstream'), _make('rtph264depay'), _make('h264parse', config_interval=-1),
                     _make('rtph264pay', pt=pt, mtu=1200, config_interval=-1, aggregate_mode='zero-latency', ssrc=self.ssrc), rtp_caps]
            self.log('relaying the camera\'s H264 as it is')
        else:
            encoder_factory, payloader_factory = _encoder_for(name) or (None, None)
            if encoder_factory is None:
                raise RuntimeError(f'no encoder for {name}')
            encode = [_make('queue', max_size_buffers=2, leaky='downstream'), _encoder(encoder_factory, self.bitrate, self.fps)]
            if name == 'H264':
                encode.append(_make('capsfilter', caps=Gst.Caps.from_string('video/x-h264,profile=constrained-baseline')))
            encode += [_make(payloader_factory, pt=pt, mtu=1200, config_interval=-1, ssrc=self.ssrc), rtp_caps]
            if self.source_encoding is not None:
                # an RTSP camera: decoded here first
                decodebin = _make('decodebin')
                raw = [_make('videoconvert'), _make('videoscale', add_borders=True), _make('videorate', drop_only=True), _make('capsfilter', caps=self._raw_caps())]
                head = [_make('queue', max_size_time=500 * Gst.MSECOND, leaky='downstream'), decodebin]
                _link_chain(self.pipeline, head)
                _link_chain(self.pipeline, raw + encode)
                decodebin.connect('pad-added', lambda element, pad: self._link_video(pad, raw[0]))
                for element in head + raw + encode:
                    element.sync_state_with_parent()
                self._tee.link(head[0])
                encode[-1].get_static_pad('src').link(self._video_pad)
                self.log(f'encoding the camera to {name} {self.width}x{self.height} {self.fps} fps, {self.bitrate} kbit/s ({encoder_factory})')
                self.webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), _checked(self.extra.label, 'using the answer'))
                return
            chain = encode
            self.log(f'sending {name} {self.width}x{self.height} {self.fps} fps, {self.bitrate} kbit/s ({encoder_factory})')
        _link_chain(self.pipeline, chain)
        if chain[-1].get_static_pad('src').link(self._video_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError('cannot link the video to webrtcbin')
        for element in chain:
            element.sync_state_with_parent()
        self._tee.link(chain[0])
        self.webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), _checked(self.extra.label, 'using the answer'))

    def stop(self):
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            pipeline.get_bus().set_sync_handler(None, None)
            pipeline.set_state(Gst.State.NULL)
        self.webrtcbin = None

    def _on_bus_message(self, bus, message, data):
        # any thread
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source = message.src.get_name() if message.src is not None else '?'
            log.debug(f'{self.extra.label}: GStreamer error in {source}: {error.message} ({debug})')
            refused = source == 'source' and re.search(r'unauthori[sz]ed|401|not found|404|could not (open|connect)|resource', f'{error.message} {debug}', re.I)
            self.extra.failed(error.message + (' (check the address, user and password)' if refused else ''))
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            log.debug(f'{self.extra.label}: GStreamer warning: {warning.message}')
        return Gst.BusSyncReply.DROP


class VideoFrame(object):
    """A decoded video frame for a frame handler (sipclient.video.VideoWindow.frame_handler): width, height, and data,
    the pixels in BGRA byte order (ARGB on macOS), rows packed."""

    __slots__ = ('width', 'height', 'data')

    def __init__(self, width, height, data):
        self.width = width
        self.height = height
        self.data = data


class CallMedia(_Strips):
    """The media of a 1 to 1 call: one webrtcbin carrying audio and video both ways.

        microphone -> [webrtcdsp] -> volume (mute) -> opusenc -> rtpopuspay ------------> webrtcbin
        camera -> decodebin -> convert/scale/rate -> encoder -> payloader (once the codec is known) -> webrtcbin
        webrtcbin -> decodebin -> audio: convert/resample -> audiomixer -> [webrtcechoprobe] -> speakers
                               -> video: convert -> the video output

    call: what is told (from any thread): local_description(sdp, kind), candidate(mline, mid, candidate),
    end_of_candidates(), ice_failed(), media_error(source, text, fatal), remote_video(width, height), video_closed().
    video_source: our camera (dict(kind='v4l2', device, resolution, fps) or dict(kind='test', pattern, ...)), or None.
    video_output: what the remote video goes to: None (not even received), 'auto' (a window of GStreamer's own),
    a GStreamer sink description, or a callable(VideoFrame) (sipclient.video.VideoWindow.frame_handler).
    video_size: (width, height) the far end's video is scaled to, its aspect kept (borders); None: as it comes.
    stereo: stereo audio: Opus in stereo (the call is stereo when the far end asks for it too), no echo canceller.
    """

    FPS = 15

    def __init__(self, call, audio_input='auto', audio_output='auto', video_source=None, video_output=None, ice_servers=None, video_size=None,
                 stereo=False, playback=None, monitor=False):
        self.call = call
        self.stereo = stereo
        self.monitor = bool(monitor or playback)    # what we send, heard here too (see _monitor_head)
        self.monitor_on = {'mic': True, 'music': True} if monitor else {}     # --monitor: heard from the start
        self.playback_gain = None                   # the music's gain at first (dB), else SendMixer's default
        self.jitter_buffer = None                   # ms of the jitter buffer (webrtcbin's latency), None: 200
        self.playback_random = False                # the music from a random file of it, not the first
        self.playback = playback        # audio files (or folders of them) mixed into what we send, see SendMixer
        self.send_mixer = None
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.video_source = video_source
        self.video_output = video_output
        self.video_size = video_size        # (width, height) the far end's video is scaled to (aspect kept, bordered), or None
        self.ice_servers = ice_servers or []
        self.pipeline = None
        self.webrtcbin = None
        self.video_codec = None         # what we send, once negotiated
        self._playback = None
        self._volume = None
        self._video_pad = None
        self._video_ssrc = random.randint(1, 0xffffffff)
        self._levels = _Levels()
        self._remote_size = None
        self._camera_linked = False

    @property
    def video(self):
        return self.video_source is not None or self.video_output is not None

    def start(self, offer=None):
        """Without an offer, make ours (call.local_description(sdp, 'offer')); with the far end's, answer it."""
        missing = media_missing()
        if missing:
            raise RuntimeError('missing ' + ', '.join(f'{what} ({package})' for what, package in missing))
        if self.video_source is not None and self.video_source.get('kind') not in ('v4l2', 'test'):
            raise RuntimeError('the camera of a call must be a local one (v4l2 or test)')
        self.pipeline = Gst.Pipeline.new('call')
        self.pipeline.use_clock(Gst.SystemClock.obtain())     # the microphone or speakers may change while playing
        self.pipeline.get_bus().set_sync_handler(self._on_bus_message, None)
        echo_cancellation = not self.stereo and self.audio_input != 'none' and self.audio_output != 'none' and _has('webrtcdsp') and _has('webrtcechoprobe')

        # one far end: its audio goes straight to the speakers, no mixer (an aggregator that outputs
        # silence and drops what comes after its deadline)
        playback = [_make('audioconvert', 'playback'), _make('audioresample')]
        if echo_cancellation:
            playback.append(_make('webrtcechoprobe', 'echoprobe'))
        playback += _monitor_head(self)
        playback.append(RoomMedia._audio_element(self, self.audio_output, 'speakers', sink=True))
        _link_chain(self.pipeline, playback)
        self._playback = playback[0]

        self.webrtcbin = webrtcbin = _make('webrtcbin', 'call', bundle_policy='max-bundle')
        _receiving(webrtcbin, self)
        _configure_ice(webrtcbin, self.ice_servers)
        self.pipeline.add(webrtcbin)
        call = self.call
        webrtcbin.connect('on-negotiation-needed', lambda element: None)
        webrtcbin.connect('on-ice-candidate', lambda element, mline, candidate: call.candidate(mline, _mid(element, mline), candidate))
        webrtcbin.connect('notify::ice-gathering-state', lambda element, spec: element.get_property('ice-gathering-state') == GstWebRTC.WebRTCICEGatheringState.COMPLETE and call.end_of_candidates())
        webrtcbin.connect('notify::ice-connection-state', lambda element, spec: element.get_property('ice-connection-state') == GstWebRTC.WebRTCICEConnectionState.FAILED and call.ice_failed())
        webrtcbin.connect('pad-added', self._on_pad)
        _watch_states(webrtcbin, call.peer)

        # what the far end offered decides the payload types of an answer
        audio_codecs = offered_codecs(offer, 'audio') if offer else []
        opus_pt = next((pt for name, pt, rate, channels, fmtp in audio_codecs if name == 'OPUS'), OPUS_PT)
        if offer and not any(name == 'OPUS' for name, pt, rate, channels, fmtp in audio_codecs):
            raise RuntimeError('the call offers no OPUS audio')

        microphone = _send_audio(self, echo_cancellation, self.call.audio_muted)
        audio_caps = _opus_caps(opus_pt, self.stereo)
        microphone += [
                       _make('queue', max_size_time=200 * Gst.MSECOND, leaky='downstream'),
                       _opus_encoder(self.stereo),
                       _make('rtpopuspay', pt=opus_pt), _make('capsfilter', caps=audio_caps)]
        _link_chain(self.pipeline, microphone)
        _attach_send_audio(self)
        audio_pad = webrtcbin.request_pad_simple('sink_%u')
        transceiver = audio_pad.get_property('transceiver')
        transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDRECV)
        transceiver.set_property('codec-preferences', audio_caps)
        if microphone[-1].get_static_pad('src').link(audio_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError('cannot link the microphone to webrtcbin')

        video_offered = has_media(offer, 'video') if offer else True
        if self.video and video_offered:
            if offer:
                ours = [name for name, pt in (_encodable_codecs() if self.video_source is not None else [(name, pt) for name, pt, encoder, payloader in VIDEO_CODECS])]
                codecs = [(name, pt, fmtp) for name, pt, rate, channels, fmtp in offered_codecs(offer, 'video') if name in ours]
                codecs.sort(key=lambda codec: ours.index(codec[0]))
                if not codecs:
                    log.warning('the call offers no video codec we can use: audio only')
                preferences = self._preferences(codecs)
            else:
                codecs = _encodable_codecs() if self.video_source is not None else [(name, pt) for name, pt, encoder, payloader in VIDEO_CODECS[:3]]
                if not codecs:
                    raise RuntimeError('no video encoder (vp9enc, vp8enc, x264enc or openh264enc)')
                preferences = _video_preferences(codecs, self._video_ssrc)
            if codecs:
                if self.video_source is not None:
                    self._video_pad = webrtcbin.request_pad_simple('sink_%u')
                    video_transceiver = self._video_pad.get_property('transceiver')
                    video_transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDRECV if self.video_output is not None
                                                   else GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
                    video_transceiver.set_property('codec-preferences', preferences)
                else:
                    webrtcbin.emit('add-transceiver', GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, preferences)

        self.pipeline.set_state(Gst.State.PLAYING)
        if self.pipeline is None:
            return
        if offer is None:
            webrtcbin.emit('create-offer', None, Gst.Promise.new_with_change_func(self._on_offer_created, None))
        else:
            webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.OFFER, offer), Gst.Promise.new_with_change_func(self._on_remote_offer_set, None))

    def _preferences(self, codecs):
        """The codec preferences answering an offer: its own payload types, our SSRC."""
        preferences = Gst.Caps.new_empty()
        for name, pt, fmtp in codecs:
            caps = (f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000,rtcp-fb-nack=(boolean)true,'
                    f'rtcp-fb-nack-pli=(boolean)true,rtcp-fb-ccm-fir=(boolean)true,ssrc=(uint){self._video_ssrc}')
            if name == 'H264':
                parameters = dict(item.strip().partition('=')[::2] for item in fmtp.split(';') if '=' in item)
                if parameters.get('packetization-mode', '0') != '1':
                    continue
                caps += f',packetization-mode=(string)1,profile-level-id=(string){parameters.get("profile-level-id", "42e01f")}'
            preferences.append(Gst.Caps.from_string(caps))
        return preferences

    def _audio_element(self, description, name, sink):
        return RoomMedia._audio_element(self, description, name, sink)

    # Negotiation

    def _on_offer_created(self, promise, data):
        # streaming thread
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        if offer is None or self.webrtcbin is None:
            self.call.media_error('call', 'cannot create the offer', True)
            return
        self.webrtcbin.emit('set-local-description', offer, Gst.Promise.new())
        text = offer.sdp.as_text()
        self.call.local_description(with_opus_stereo(text) if self.stereo else text, 'offer')

    def set_answer(self, sdp):
        """The far end's answer to our offer."""
        offer = self.webrtcbin.get_property('local-description')
        if offer is not None:
            completed = complete_answer(offer.sdp.as_text(), sdp)
            if completed != sdp:
                log.info(f'{self.call.peer}: the answer leaves out media of our offer: taken as rejected')
                sdp = completed
        if self._video_pad is not None:
            self._link_our_video(sdp)
        self.webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), _checked(self.call.peer, 'using the answer'))

    def _link_our_video(self, sdp):
        if self._camera_linked:
            return
        codec = chosen_video_codec(sdp)
        if codec is None:
            log.info('the call takes no video from us')
            return
        self._camera_linked = True
        self.video_codec = codec[0]
        _link_camera(self.pipeline, self._video_pad, self.video_source, codec[0], codec[1], self._video_ssrc, self.FPS, 'call')

    def _on_remote_offer_set(self, promise, data):
        # streaming thread
        promise.wait()
        if self.webrtcbin is None:
            return
        self.webrtcbin.emit('create-answer', None, Gst.Promise.new_with_change_func(self._on_answer_created, None))

    def _on_answer_created(self, promise, data):
        # streaming thread
        promise.wait()
        reply = promise.get_reply()
        answer = reply.get_value('answer') if reply is not None else None
        if answer is None or self.webrtcbin is None:
            self.call.media_error('call', 'cannot answer the offer', True)
            return
        text = answer.sdp.as_text()
        if self._video_pad is not None:
            try:
                self._link_our_video(text)
            except Exception as e:
                self.call.media_error('camera', str(e), False)
        self.webrtcbin.emit('set-local-description', answer, Gst.Promise.new())
        self.call.local_description(with_opus_stereo(text) if self.stereo else text, 'answer')

    def add_remote_candidate(self, mline, candidate):
        if self.webrtcbin is not None:
            self.webrtcbin.emit('add-ice-candidate', mline, candidate)

    # What comes in

    def _on_pad(self, webrtcbin, pad):
        # streaming thread
        if pad.get_direction() != Gst.PadDirection.SRC or self.pipeline is None:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        media = caps.get_structure(0).get_string('media') if caps is not None and caps.get_size() else None
        if media == 'video' and self.video_output is None:
            self._discard(pad)
            return
        if media == 'audio':
            clock_rate = caps.get_structure(0).get_value('clock-rate') if caps.get_structure(0).has_field('clock-rate') else 48000
            self._audio_clock = dict(rate=clock_rate or 48000, start=None, last_log=None, packets=0, gaps=0, last=None)
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_received_audio)
        decodebin = _make('decodebin')
        decodebin.connect('pad-added', self._on_decoded_pad)
        decodebin.connect('deep-element-added', RoomMedia._on_feed_element_added)
        self._add_branch([decodebin], pad)

    def _on_received_audio(self, pad, info):
        # streaming thread: the received audio's RTP clock against the wall clock, every 5 s. Audio played
        # with gaps while nothing is lost comes from a sender whose timestamps do not advance at the rate it
        # names (clock ratio far from 1.00), or from gaps in its sending (the longest gap)
        buffer = info.get_buffer()
        clock = getattr(self, '_audio_clock', None)
        if buffer is None or clock is None:
            return Gst.PadProbeReturn.OK
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            data = bytes(mapped.data[:12])
        finally:
            buffer.unmap(mapped)
        if len(data) < 12:
            return Gst.PadProbeReturn.OK
        timestamp = int.from_bytes(data[4:8], 'big')
        now = time.monotonic()
        if clock['start'] is None:
            clock['start'] = clock['last_log'] = (now, timestamp)
        if clock['last'] is not None:
            clock['gaps'] = max(clock['gaps'], now - clock['last'])
        clock['last'] = now
        clock['packets'] += 1
        then, then_timestamp = clock['last_log']
        if now - then >= 5:
            elapsed = now - then
            rtp_elapsed = ((timestamp - then_timestamp) & 0xffffffff) / clock['rate']
            log.info(f'call: received audio: {clock["packets"]} packets in {elapsed:.1f} s, RTP clock {rtp_elapsed:.2f} s '
                     f'(ratio {rtp_elapsed / elapsed:.2f}), longest gap {clock["gaps"] * 1000:.0f} ms')
            clock['last_log'] = (now, timestamp)
            clock['packets'] = 0
            clock['gaps'] = 0
        return Gst.PadProbeReturn.OK

    def _add_branch(self, elements, pad):
        for element in elements:
            self.pipeline.add(element)
        _link_chain(self.pipeline, elements, add=False)
        for element in elements:
            element.sync_state_with_parent()
        pad.link(elements[0].get_static_pad('sink'))

    def _discard(self, pad):
        sink = _make('fakesink', sync=False)
        sink.set_property('async', False)
        self._add_branch([sink], pad)

    def _on_decoded_pad(self, decodebin, pad):
        # streaming thread
        if self.pipeline is None:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        kind = caps.get_structure(0).get_name() if caps is not None and caps.get_size() else ''
        if kind.startswith('audio/'):
            chain = [_make('queue', max_size_buffers=0, max_size_bytes=0, max_size_time=RECEIVED_AUDIO_QUEUE * Gst.SECOND, leaky='downstream'), _make('audioconvert'), _make('audioresample'),
                     _make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,rate=48000,channels=2'))]
            chain += [self._listen_volume('remote', 'listen-remote')] + _level('level-remote')
            self._remote_channels = _channels(caps)
            playback = self._playback.get_static_pad('sink')
            if playback.is_linked():
                self._discard(pad)      # a second audio stream: not expected in a call
                return
            self._add_branch(chain, pad)
            chain[-1].get_static_pad('src').link(playback)
        elif kind.startswith('video/') and self.video_output is not None:
            structure = caps.get_structure(0)
            if structure.has_field('width') and structure.has_field('height'):
                self._remote_size = (structure.get_value('width'), structure.get_value('height'))
                self.call.remote_video(*self._remote_size)
            chain = [_make('queue', max_size_buffers=2, leaky='downstream'), _make('videoconvert')]
            if self.video_size is not None:
                width, height = self.video_size
                chain += [_make('videoscale', add_borders=True),
                          _make('capsfilter', caps=Gst.Caps.from_string(f'video/x-raw,width={width},height={height},pixel-aspect-ratio=1/1'))]
            self._add_branch(chain + self._video_output(), pad)
        else:
            self._discard(pad)

    def _video_output(self):
        output = self.video_output
        if callable(output):
            import sys
            pixel_format = 'ARGB' if sys.platform == 'darwin' else 'BGRA'
            sink = _make('appsink', 'remote-video', emit_signals=True, max_buffers=1, drop=True, sync=False,
                         caps=Gst.Caps.from_string(f'video/x-raw,format={pixel_format}'))
            sink.connect('new-sample', self._on_sample)
            return [sink]
        if output in ('auto', '', None):
            return [_make('autovideosink', 'remote-video', sync=False)]
        element = Gst.parse_bin_from_description(output, True)
        element.set_name('remote-video')
        return [element]

    def _on_sample(self, sink):
        # streaming thread
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK
        structure = sample.get_caps().get_structure(0)
        width, height = structure.get_value('width'), structure.get_value('height')
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buffer.unmap(info)
        stride = len(data) // height if height else 0
        if stride and stride != width * 4:      # padded rows
            data = b''.join(data[row * stride:row * stride + width * 4] for row in range(height))
        try:
            self.video_output(VideoFrame(width, height, data))
        except Exception as e:
            log.debug(f'video output: {e}')
        return Gst.FlowReturn.OK

    # Controls

    def set_audio_muted(self, muted):
        if self._volume is not None:
            self._volume.set_property('mute', bool(muted))

    def set_audio_device(self, kind, description):
        """Other speakers (kind output) or another microphone (input): auto, none, a GStreamer description or an
        AudioDevice; replaced in the running pipeline."""
        _replace_audio(self, kind, description)

    def stop(self):
        if self.pipeline is not None:
            pipeline, self.pipeline = self.pipeline, None
            pipeline.get_bus().set_sync_handler(None, None)
            pipeline.set_state(Gst.State.NULL)
        self.webrtcbin = None

    def stats(self):
        """[(label, direction, stats)]: what goes out and what comes in."""
        if self.webrtcbin is None:
            return []
        stats = webrtcbin_stats(self.webrtcbin)
        out = dict(stats, received_bytes=0, received_packets=0, lost_received=0, jitter_received=None, late_received=0)
        incoming = dict(stats, sent_bytes=0, sent_packets=0, lost_sent=None, fraction_lost_sent=None, rtt=None, jitter_sent=None)
        return [('audio+video' if self._camera_linked else 'audio', 'out', out),
                ('audio+video' if self._remote_size else 'audio', 'in', incoming)]

    def _listen(self):
        return [('remote', self.call.peer)]

    def levels(self):
        """{(label, direction): (RMS dBFS, peak dBFS, per channel)} since the last call, labelled as in stats(): the
        far end's channels as it sends them (1 or 2)."""
        found = {}
        for name, key in (('miclevel', ('audio+video' if self._camera_linked else 'audio', 'out')),
                          ('level-remote', ('audio+video' if self._remote_size else 'audio', 'in'))):
            level = self._levels.take(name)
            if name == 'level-remote':
                level = _as_channels(level, getattr(self, '_remote_channels', None))
            if level is not None:
                found[key] = level
        if self.send_mixer is not None:
            found.update(self.send_mixer.levels())
        return found

    def _on_bus_message(self, bus, message, data):
        # any thread
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source = message.src.get_name() if message.src is not None else '?'
            if source.startswith('remote-video') and 'window was closed' in (error.message or '').lower():
                self.call.video_closed()        # GStreamer's own window, closed by the user
                return Gst.BusSyncReply.DROP
            fatal = source.startswith(('call', 'microphone', 'speakers', 'mixer'))     # the camera or the video window failing is not
            log.debug(f'call: GStreamer error in {source}: {error.message} ({debug})')
            self.call.media_error(source, error.message, fatal)
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            log.debug(f'call: GStreamer warning in {message.src.get_name() if message.src else "?"}: {warning.message}')
        elif message.type == Gst.MessageType.ELEMENT and message.src is not None:
            self._levels.add(message.get_structure(), message.src.get_name())
        return Gst.BusSyncReply.DROP


# Statistics

def webrtcbin_stats(webrtcbin):
    """What a webrtcbin sent and received so far: dict(sent_bytes, sent_packets, received_bytes, received_packets,
    lost_received, lost_sent (cumulative, None when not reported), fraction_lost_sent (last report), rtt, jitter_received
    (ours), jitter_sent (the far end's report) (seconds, None when unknown), late_received (packets that came after
    their time, dropped by the jitter buffer: heard as gaps, not loss)). Blocks for the answer (it is quick)."""
    totals = dict(sent_bytes=0, sent_packets=0, received_bytes=0, received_packets=0, lost_received=0, lost_sent=None,
                  fraction_lost_sent=None, rtt=None, jitter_received=None, jitter_sent=None, late_received=0)
    if webrtcbin is None:
        return totals
    for jitterbuffer in list(getattr(webrtcbin, '_jitterbuffers', [])):
        try:
            stats = jitterbuffer.get_property('stats')
            totals['late_received'] += stats.get_value('num-late') or 0
        except Exception:
            pass
    promise = Gst.Promise.new()
    webrtcbin.emit('get-stats', None, promise)
    if promise.wait() != Gst.PromiseResult.REPLIED:
        return totals
    reply = promise.get_reply()
    if reply is None:
        return totals

    def number(structure, name):
        if not structure.has_field(name):
            return None
        try:
            return structure.get_value(name)
        except (TypeError, ValueError):
            return None

    def field(field_id, value):
        if not isinstance(value, Gst.Structure) or not value.has_field('type'):
            return True
        kind = getattr(value.get_value('type'), 'value_nick', '')
        if kind == 'outbound-rtp':
            totals['sent_bytes'] += number(value, 'bytes-sent') or 0
            totals['sent_packets'] += number(value, 'packets-sent') or 0
        elif kind == 'inbound-rtp':
            totals['received_bytes'] += number(value, 'bytes-received') or 0
            totals['received_packets'] += number(value, 'packets-received') or 0
            totals['lost_received'] += max(0, number(value, 'packets-lost') or 0)
            jitter = number(value, 'jitter')
            if jitter is not None and jitter >= 0:
                totals['jitter_received'] = max(jitter, totals['jitter_received'] or 0)
        elif kind == 'remote-inbound-rtp':      # the far end's receiver reports about what we send
            lost = number(value, 'packets-lost')
            if lost is not None and lost >= 0:
                totals['lost_sent'] = (totals['lost_sent'] or 0) + lost
            fraction = number(value, 'fraction-lost')
            if fraction is not None:
                totals['fraction_lost_sent'] = max(fraction, totals['fraction_lost_sent'] or 0)
            rtt = number(value, 'round-trip-time')
            if rtt is not None and rtt > 0:
                totals['rtt'] = max(rtt, totals['rtt'] or 0)
            jitter = number(value, 'jitter')
            if jitter is not None and jitter >= 0:
                totals['jitter_sent'] = max(jitter, totals['jitter_sent'] or 0)
        return True

    reply.foreach(field)
    return totals
