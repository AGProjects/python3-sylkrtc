# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""A client of SylkServer's WebRTC gateway.

SylkServer publishes its infrastructure in a configuration document found by
the DNS TXT record _sylkserver.<domain> (sylkrtc.discovery). Its webrtcgateway
speaks sylkRTC over a WebSocket (sylkrtc.connection) and gives an account the
video rooms, a Janus SFU (sylkrtc.videoroom), 1 to 1 calls and its addressbook
(sylkrtc.call, sylkrtc.addressbook). The media is done by a backend: GStreamer
webrtcbin on Linux (sylkrtc.gst).

Everything runs in the twisted reactor thread; the results are posted with the
NotificationCenter of python3-application. Logging goes to the 'sylkrtc' logger.
"""

__version__ = '1.0.0'

PROTOCOL = 'sylkRTC-2'

__all__ = ['__version__', 'PROTOCOL']
