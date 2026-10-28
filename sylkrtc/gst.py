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
import random
import re
import time

from sylkrtc.sdp import chosen_video_codec, cvo_id, has_media, ice_urls, mids, offered_codecs, with_cvo


__all__ = ['MediaFactory', 'RoomMedia', 'ExtraMedia', 'CallMedia', 'VideoFrame', 'media_missing', 'media_report', 'RESOLUTIONS', 'parse_resolution', 'webrtcbin_stats']


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
LEVEL_INTERVAL = 150 * 1000000  # ns between level messages

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
        Gst.init(None)
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


def _link_chain(pipeline, elements, add=True):
    if add:
        for element in elements:
            pipeline.add(element)
    for left, right in zip(elements, elements[1:]):
        if not left.link(right):
            raise RuntimeError(f'cannot link {left.get_name()} to {right.get_name()}')


def _sdp_message(text):
    result, message = GstSdp.SDPMessage.new_from_text(text)
    if result != GstSdp.SDPResult.OK:
        raise ValueError('unreadable SDP')
    return message


def _description(kind, text):
    return GstWebRTC.WebRTCSessionDescription.new(kind, _sdp_message(text))


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
    """

    def __init__(self, audio_input='auto', audio_output='auto', video_sink=None, video_source=None):
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.video_sink = video_sink
        self.video_source = video_source

    def __call__(self, session):
        return RoomMedia(session, self.audio_input, self.audio_output, self.video_sink, self.video_source)


class RoomMedia(object):
    """video_source: our own video (session.video), as an extra video's source: dict(kind='v4l2', device, resolution, fps)
    or dict(kind='test', pattern, resolution, fps); the extra videos are shown under it."""

    MAIN_FPS = 15

    def __init__(self, session, audio_input='auto', audio_output='auto', video_sink=None, video_source=None):
        self.session = session
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.video_sink = video_sink
        self.video_source = video_source
        self.pipeline = None
        self.publisher = None
        self._mixer = None
        self._volume = None
        self._video_pad = None
        self._video_ssrc = random.randint(1, 0xffffffff)

    # The publisher

    def start(self):
        missing = media_missing()
        if missing:
            raise RuntimeError('missing ' + ', '.join(f'{what} ({package})' for what, package in missing))
        if self.session.video and (self.video_source or {}).get('kind') not in ('v4l2', 'test'):
            raise RuntimeError('our own video must be a local camera (v4l2 or test)')
        self.pipeline = Gst.Pipeline.new('videoroom')
        self.pipeline.get_bus().set_sync_handler(self._on_bus_message, None)
        echo_cancellation = self.audio_input != 'none' and self.audio_output != 'none' and _has('webrtcdsp') and _has('webrtcechoprobe')

        # what is played: every feed's audio, mixed
        self._mixer = _make('audiomixer', 'mixer', ignore_inactive_pads=True)
        playback = [self._mixer, _make('audioconvert'), _make('audioresample')]
        if echo_cancellation:
            playback.append(_make('webrtcechoprobe', 'echoprobe'))
        playback.append(self._audio_element(self.audio_output, 'speakers', sink=True))
        _link_chain(self.pipeline, playback)

        self.publisher = webrtcbin = _make('webrtcbin', 'publisher', bundle_policy='max-bundle')
        _configure_ice(webrtcbin, self.session.ice_servers)
        self.pipeline.add(webrtcbin)
        self._watch_webrtcbin(webrtcbin, self.session.id)

        microphone = [self._audio_element(self.audio_input, 'microphone', sink=False), _make('audioconvert'), _make('audioresample')]
        if echo_cancellation:
            microphone += [_make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,rate=48000,channels=1')), _make('webrtcdsp', 'dsp', probe='echoprobe')]
        self._volume = _make('volume', 'micvolume', mute=self.session.audio_muted)
        microphone += [self._volume, _make('audioconvert'), _make('audioresample'),
                       _make('queue', max_size_time=200 * Gst.MSECOND, leaky='downstream'),
                       _make('opusenc', bitrate=32000, inband_fec=True, audio_type='voice'),
                       _make('rtpopuspay', pt=OPUS_PT)]
        audio_caps = Gst.Caps.from_string(f'application/x-rtp,media=audio,encoding-name=OPUS,payload={OPUS_PT},clock-rate=48000,encoding-params=(string)2')
        microphone.append(_make('capsfilter', caps=audio_caps))
        _link_chain(self.pipeline, microphone)
        audio_pad = webrtcbin.request_pad_simple('sink_%u')
        transceiver = audio_pad.get_property('transceiver')
        transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
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
        self.session.publisher_offer(offer.sdp.as_text())

    def set_publisher_answer(self, sdp):
        if self._video_pad is not None:
            codec = chosen_video_codec(sdp)
            if codec is None:
                log.warning(f'{self.session.room}: the room takes no video from us')
            else:
                self._link_camera(*codec)
        self.publisher.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), Gst.Promise.new())

    def _link_camera(self, name, pt):
        _link_camera(self.pipeline, self._video_pad, self.video_source, name, pt, self._video_ssrc, self.MAIN_FPS, self.session.room)

    def set_audio_muted(self, muted):
        if self._volume is not None:
            self._volume.set_property('mute', bool(muted))

    def stop(self):
        if self.pipeline is not None:
            pipeline, self.pipeline = self.pipeline, None
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_bus().set_sync_handler(None, None)
        self.publisher = None

    # ICE and the bus

    def _watch_webrtcbin(self, webrtcbin, label):
        session = self.session
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
        elif message.type == Gst.MessageType.ELEMENT and message.src is not None and message.src.get_name().startswith('level-'):
            structure = message.get_structure()
            if structure is not None and structure.get_name() == 'level':
                try:
                    peaks = structure.get_value('peak')
                    peak = max(peaks) if peaks else -100.0
                except (TypeError, ValueError):
                    peak = -100.0
                self.session.level(message.src.get_name()[6:], peak > SPEAKING_DB)
        return Gst.BusSyncReply.DROP

    # Feeds

    def add_feed(self, participant, sdp):
        webrtcbin = _make('webrtcbin', 'feed-' + participant.feed, bundle_policy='max-bundle')
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
            if _has('level'):
                chain.append(_make('level', 'level-' + participant.feed, interval=LEVEL_INTERVAL, post_messages=True))
            self._add_branch(participant, chain, pad)
            mixer_pad = self._mixer.request_pad_simple('sink_%u')
            chain[-1].get_static_pad('src').link(mixer_pad)
            participant.media['mixer_pads'].append(mixer_pad)
        elif kind.startswith('video/') and self.video_sink is not None:
            self._add_branch(participant, [_make('queue', max_size_buffers=2, leaky='downstream'), _make('videoconvert'), self.video_sink(participant)], pad)
        else:
            self._discard(participant, pad)

    def remove_feed(self, participant):
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
                self.webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), Gst.Promise.new())
                return
            chain = encode
            self.log(f'sending {name} {self.width}x{self.height} {self.fps} fps, {self.bitrate} kbit/s ({encoder_factory})')
        _link_chain(self.pipeline, chain)
        if chain[-1].get_static_pad('src').link(self._video_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError('cannot link the video to webrtcbin')
        for element in chain:
            element.sync_state_with_parent()
        self._tee.link(chain[0])
        self.webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), Gst.Promise.new())

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


class CallMedia(object):
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
    """

    FPS = 15

    def __init__(self, call, audio_input='auto', audio_output='auto', video_source=None, video_output=None, ice_servers=None, video_size=None):
        self.call = call
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
        self.pipeline.get_bus().set_sync_handler(self._on_bus_message, None)
        echo_cancellation = self.audio_input != 'none' and self.audio_output != 'none' and _has('webrtcdsp') and _has('webrtcechoprobe')

        # one far end: its audio goes straight to the speakers, no mixer (an aggregator that outputs
        # silence and drops what comes after its deadline)
        playback = [_make('audioconvert', 'playback'), _make('audioresample')]
        if echo_cancellation:
            playback.append(_make('webrtcechoprobe', 'echoprobe'))
        playback.append(RoomMedia._audio_element(self, self.audio_output, 'speakers', sink=True))
        _link_chain(self.pipeline, playback)
        self._playback = playback[0]

        self.webrtcbin = webrtcbin = _make('webrtcbin', 'call', bundle_policy='max-bundle')
        _configure_ice(webrtcbin, self.ice_servers)
        self.pipeline.add(webrtcbin)
        call = self.call
        webrtcbin.connect('on-negotiation-needed', lambda element: None)
        webrtcbin.connect('on-ice-candidate', lambda element, mline, candidate: call.candidate(mline, _mid(element, mline), candidate))
        webrtcbin.connect('notify::ice-gathering-state', lambda element, spec: element.get_property('ice-gathering-state') == GstWebRTC.WebRTCICEGatheringState.COMPLETE and call.end_of_candidates())
        webrtcbin.connect('notify::ice-connection-state', lambda element, spec: element.get_property('ice-connection-state') == GstWebRTC.WebRTCICEConnectionState.FAILED and call.ice_failed())
        webrtcbin.connect('pad-added', self._on_pad)

        # what the far end offered decides the payload types of an answer
        audio_codecs = offered_codecs(offer, 'audio') if offer else []
        opus_pt = next((pt for name, pt, rate, channels, fmtp in audio_codecs if name == 'OPUS'), OPUS_PT)
        if offer and not any(name == 'OPUS' for name, pt, rate, channels, fmtp in audio_codecs):
            raise RuntimeError('the call offers no OPUS audio')

        microphone = [RoomMedia._audio_element(self, self.audio_input, 'microphone', sink=False), _make('audioconvert'), _make('audioresample')]
        if echo_cancellation:
            microphone += [_make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,rate=48000,channels=1')), _make('webrtcdsp', 'dsp', probe='echoprobe')]
        self._volume = _make('volume', 'micvolume', mute=self.call.audio_muted)
        audio_caps = Gst.Caps.from_string(f'application/x-rtp,media=audio,encoding-name=OPUS,payload={opus_pt},clock-rate=48000,encoding-params=(string)2')
        microphone += [self._volume, _make('audioconvert'), _make('audioresample'),
                       _make('queue', max_size_time=200 * Gst.MSECOND, leaky='downstream'),
                       _make('opusenc', bitrate=32000, inband_fec=True, audio_type='voice'),
                       _make('rtpopuspay', pt=opus_pt), _make('capsfilter', caps=audio_caps)]
        _link_chain(self.pipeline, microphone)
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
        self.call.local_description(offer.sdp.as_text(), 'offer')

    def set_answer(self, sdp):
        """The far end's answer to our offer."""
        if self._video_pad is not None:
            self._link_our_video(sdp)
        self.webrtcbin.emit('set-remote-description', _description(GstWebRTC.WebRTCSDPType.ANSWER, sdp), Gst.Promise.new())

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
        self.call.local_description(text, 'answer')

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
        out = dict(stats, received_bytes=0, received_packets=0, lost_received=0)
        incoming = dict(stats, sent_bytes=0, sent_packets=0, lost_sent=None, fraction_lost_sent=None, rtt=None)
        return [('audio+video' if self._camera_linked else 'audio', 'out', out),
                ('audio+video' if self._remote_size else 'audio', 'in', incoming)]

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
        return Gst.BusSyncReply.DROP


# Statistics

def webrtcbin_stats(webrtcbin):
    """What a webrtcbin sent and received so far: dict(sent_bytes, sent_packets, received_bytes, received_packets,
    lost_received, lost_sent (cumulative, None when not reported), fraction_lost_sent (last report), rtt (seconds, None
    when unknown)). Blocks for the answer (it is quick)."""
    totals = dict(sent_bytes=0, sent_packets=0, received_bytes=0, received_packets=0, lost_received=0, lost_sent=None,
                  fraction_lost_sent=None, rtt=None)
    if webrtcbin is None:
        return totals
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
        return True

    reply.foreach(field)
    return totals
