# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""The settings of the sylk clients in the SDK's configuration (~/.sylkclient/config).

    SylkSettings:                       general
        audio_input = auto
        audio_output = auto
        muted = false
        tls_verify_server = false
        default_session = home
        device_id = 7c6150fd-...        ; this computer to SylkServer, made at the first use of any tool (sylkrtc.device)

    Sessions:                           what a room is joined with, by room (one session per room)
        myroom:                         a room name, room@domain or a room link
            name = home
            account = alice@example.com
            cameras = Garden, Desk      ; by name or id
            muted = true                ; overrides the general setting (audio_input, audio_output too)

    RTSPCameras:                        network cameras, usable by any session
        default:                        the credentials of the RTSP cameras without their own
            user = admin
            password = secret
        a4-14-37-0b-cc-12:              its MAC address when known (cameraN otherwise)
            name = Garden
            url = rtsp://192.168.1.20/stream1

    LocalCameras:                       video devices of this machine, usable by any session
        local1:
            name = Desk
            device = /dev/video0

    Accounts:                           the SDK's own
        ...

These classes write it (sylk-settings, with the SDK's configuration manager);
sylkrtc.sipconfig reads the same data without the SDK.
"""

import re

from sipsimple.configuration import ConfigurationManager, Setting, SettingsObject, SettingsObjectID
from sipsimple.configuration.datatypes import List


__all__ = ['SylkSettings', 'Session', 'RTSPCamera', 'LocalCamera', 'CameraDefaults', 'RESERVED_ID', 'rtsp_cameras', 'local_cameras', 'NameList', 'Transcode', 'VideoSize',
           'sessions', 'cameras', 'create', 'change_id', 'find_session', 'find_camera', 'new_id', 'TRANSCODE_VALUES', 'MAX_CAMERAS']


TRANSCODE_VALUES = ('auto', 'yes', 'no')
MAX_CAMERAS = 4     # what one participant can publish in a room (server limit)


class NameList(List):
    type = str


class Transcode(str):
    def __new__(cls, value):
        value = str(value).lower()
        if value not in TRANSCODE_VALUES:
            raise ValueError('transcode must be one of %s' % ', '.join(TRANSCODE_VALUES))
        return str.__new__(cls, value)


class VideoSize(str):
    def __new__(cls, value):
        value = str(value).lower()
        if not re.match(r'^\d+x\d+$', value):
            raise ValueError('resolution must be WxH, like 640x360')
        return str.__new__(cls, value)


class SylkSettings(SettingsObject):
    __id__ = 'SylkSettings'

    audio_input = Setting(type=str, default='auto')                 # auto, none or a GStreamer source description
    audio_output = Setting(type=str, default='auto')                # auto, none or a GStreamer sink description
    muted = Setting(type=bool, default=False)                       # the microphone starts muted
    tls_verify_server = Setting(type=bool, default=False)
    default_session = Setting(type=str, default=None, nillable=True)    # joined when none is given
    device_id = Setting(type=str, default=None, nillable=True)          # this computer to SylkServer, made at the first use of any tool (sylkrtc.device)


class _NamedObject(SettingsObject):
    __id__ = SettingsObjectID(type=str)

    name = Setting(type=str, default=None, nillable=True)

    def __init__(self, id):
        pass

    @property
    def id(self):
        return self.__id__

    @property
    def label(self):
        return self.name or self.__id__


class Session(_NamedObject):
    """Its id is its room (a room name, room@domain or a room link): one session per room."""

    __group__ = 'Sessions'

    account = Setting(type=str, default=None, nillable=True)        # None: the default account
    cameras = Setting(type=NameList, default=None, nillable=True)   # names or ids of Cameras
    audio_input = Setting(type=str, default=None, nillable=True)    # None: the general setting
    audio_output = Setting(type=str, default=None, nillable=True)
    muted = Setting(type=bool, default=None, nillable=True)


RESERVED_ID = 'default'     # RTSPCameras: default holds the credentials of the RTSP cameras without their own


class CameraDefaults(SettingsObject):
    """What an RTSP camera without credentials of its own (in its settings or its address) uses: RTSPCameras: default."""

    __group__ = 'RTSPCameras'
    __id__ = RESERVED_ID

    user = Setting(type=str, default=None, nillable=True)
    password = Setting(type=str, default=None, nillable=True)


Session.room = property(lambda self: self.__id__)


class RTSPCamera(_NamedObject):
    """A network camera; its id is its MAC address when known (cameraN otherwise)."""

    __group__ = 'RTSPCameras'
    __prefix__ = 'camera'

    model = Setting(type=str, default=None, nillable=True)          # what the camera calls itself (ONVIF name and hardware), set by discover
    enabled = Setting(type=bool, default=True)
    url = Setting(type=str, default=None, nillable=True)            # rtsp://
    user = Setting(type=str, default=None, nillable=True)
    password = Setting(type=str, default=None, nillable=True)
    transcode = Setting(type=Transcode, default=Transcode('auto'))
    resolution = Setting(type=VideoSize, default=None, nillable=True)
    fps = Setting(type=int, default=None, nillable=True)

    @property
    def source(self):
        return self.url


class LocalCamera(_NamedObject):
    """A video device of this machine (V4L2), or a test pattern."""

    __group__ = 'LocalCameras'
    __prefix__ = 'local'

    model = Setting(type=str, default=None, nillable=True)          # what the device calls itself (V4L2 card), set by discover
    enabled = Setting(type=bool, default=True)
    device = Setting(type=str, default=None, nillable=True)         # /dev/videoN
    pattern = Setting(type=str, default=None, nillable=True)        # a GStreamer test pattern instead of a device
    resolution = Setting(type=VideoSize, default=None, nillable=True)
    fps = Setting(type=int, default=None, nillable=True)

    @property
    def source(self):
        return self.device or ('test:%s' % self.pattern if self.pattern else None)


_instances = {}      # (class, id) -> object: the SDK allows one object per id


def _object(cls, id):
    item = _instances.get((cls, id))
    if item is None or item.__state__ == 'deleted':
        item = _instances[(cls, id)] = cls(id)
    return item


def _objects(cls):
    return [_object(cls, id) for id in ConfigurationManager().get_names([cls.__group__]) if id != RESERVED_ID]


def create(cls, name, id=None):
    """A new Session or Camera with this name, and id (a camera's MAC address) or else the first free one;
    saved by the caller."""
    if id is None or id == RESERVED_ID or id in ConfigurationManager().get_names([cls.__group__]):
        id = new_id(cls)
    item = _object(cls, id)
    item.name = name
    return item


def change_id(item, id):
    """Give an object another id (a camera its MAC address); the configuration is renamed when it is saved."""
    old = item.id
    item.__id__ = id
    _instances.pop((type(item), old), None)
    _instances[(type(item), id)] = item


def sessions():
    return _objects(Session)


def rtsp_cameras():
    return _objects(RTSPCamera)


def local_cameras():
    return _objects(LocalCamera)


def cameras():
    """Every camera: the RTSP ones, then the local ones."""
    return rtsp_cameras() + local_cameras()


def _find(cls, name_or_id):
    mac = _mac(name_or_id)
    for item in _objects(cls):
        if item.id == name_or_id or (item.name or '').lower() == name_or_id.lower() or (mac is not None and _mac(item.id) == mac):
            return item
    return None


def _mac(text):
    from sylkrtc.onvif import normalize_mac
    return normalize_mac(text)


def find_session(name_or_id):
    """The session with this name (case insensitive) or id, or None."""
    return _find(Session, name_or_id)


def find_camera(name_or_id):
    """The camera (RTSP or local) with this name (case insensitive) or id (an RTSP camera's MAC address in any form), or None."""
    return _find(RTSPCamera, name_or_id) or _find(LocalCamera, name_or_id)


def new_id(cls):
    """The first free id of the group: camera1, camera2 ... / session1 ..."""
    used = set(ConfigurationManager().get_names([cls.__group__]))
    prefix = getattr(cls, '__prefix__', cls.__name__.lower())
    number = 1
    while '%s%d' % (prefix, number) in used:
        number += 1
    return '%s%d' % (prefix, number)
