# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""The video capture devices of this machine (Linux V4L2), without GStreamer.

devices() lists /sys/class/video4linux, asks each /dev/videoN what it can do
(VIDIOC_QUERYCAP) and keeps those that capture video: a camera usually has a
second node for its metadata, which is left out. Each comes with the stable
/dev/v4l/by-id path when there is one (the /dev/videoN numbers depend on the
order the devices were found in). A device is external when its USB port is
removable (sysfs), built in when it is fixed.
"""

import fcntl
import os
import struct


__all__ = ['Device', 'devices', 'preferred']


VIDIOC_QUERYCAP = 0x80685600            # _IOR('V', 0, struct v4l2_capability), 104 bytes
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_CAP_DEVICE_CAPS = 0x80000000


BUILT_IN_WORDS = ('integrated', 'built-in', 'builtin', 'internal', 'facetime')


class Device(object):
    def __init__(self, node, name, driver, bus, stable_path, removable=None):
        self.node = node                # /dev/videoN
        self.name = name                # what the device calls itself
        self.driver = driver
        self.bus = bus
        self.stable_path = stable_path  # /dev/v4l/by-id/... or None
        self.removable = removable      # the USB port's removable attribute: removable, fixed, unknown, or None

    @property
    def external(self):
        """A camera plugged in (a removable USB port), not the one built into the machine."""
        if self.removable in ('removable', 'fixed'):
            return self.removable == 'removable'
        return not any(word in self.name.lower() for word in BUILT_IN_WORDS)

    @property
    def path(self):
        """The path to configure: the stable one when there is one."""
        return self.stable_path or self.node

    def __repr__(self):
        return f'<Device {self.node} {self.name}>'


def _capabilities(node):
    """(card, driver, bus info, capabilities of this node), or None when it cannot be asked."""
    try:
        descriptor = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        data = fcntl.ioctl(descriptor, VIDIOC_QUERYCAP, bytes(104))
    except OSError:
        return None
    finally:
        os.close(descriptor)
    driver, card, bus, version, capabilities, device_caps = struct.unpack('16s32s32sIII', data[:92])
    caps = device_caps if capabilities & V4L2_CAP_DEVICE_CAPS else capabilities
    text = lambda raw: raw.split(b'\0', 1)[0].decode('utf-8', 'replace').strip()
    return text(card), text(driver), text(bus), caps


def _removable(name):
    """The removable attribute (removable, fixed, unknown) of the USB device a /sys/class/video4linux entry belongs to, or None."""
    try:
        path = os.path.realpath(f'/sys/class/video4linux/{name}/device')
    except OSError:
        return None
    for folder in (path, os.path.dirname(path)):         # the USB interface, then its device
        try:
            with open(os.path.join(folder, 'removable')) as file:
                return file.read().strip() or None
        except OSError:
            continue
    return None


def _stable_paths():
    """{/dev/videoN: /dev/v4l/by-id/...}, the first (index0) link of each."""
    found = {}
    folder = '/dev/v4l/by-id'
    try:
        names = sorted(os.listdir(folder), key=lambda name: ('index0' not in name, name))
    except OSError:
        return found
    for name in names:
        path = os.path.join(folder, name)
        found.setdefault(os.path.realpath(path), path)
    return found


def devices():
    """The video capture devices, in /dev/videoN order; [] when there are none or this is not Linux."""
    try:
        nodes = sorted((name for name in os.listdir('/sys/class/video4linux') if name.startswith('video')), key=lambda name: int(name[5:] or 0))
    except (OSError, ValueError):
        return []
    stable = _stable_paths()
    found = []
    for name in nodes:
        node = '/dev/' + name
        answer = _capabilities(node)
        if answer is None:
            continue
        card, driver, bus, caps = answer
        if not caps & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE):
            continue        # a metadata or output node
        found.append(Device(node, card or name, driver, bus, stable.get(node), _removable(name)))
    return found


def preferred(found=None):
    """The capture devices, external ones (plugged in) first, then the built-in ones."""
    found = devices() if found is None else found
    return sorted(found, key=lambda device: not device.external)
