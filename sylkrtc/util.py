# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""Running things in the reactor thread."""

from functools import wraps

from twisted.internet import reactor
from twisted.python.threadable import isInIOThread


__all__ = ['call_in_reactor', 'run_in_reactor', 'call_later', 'Timer', 'RepeatingTimer']


def call_in_reactor(function, *args, **kw):
    """Call function in the reactor thread: now when already there, else as soon as the reactor can."""
    if isInIOThread():
        function(*args, **kw)
    else:
        reactor.callFromThread(function, *args, **kw)


def run_in_reactor(function):
    @wraps(function)
    def wrapper(*args, **kw):
        call_in_reactor(function, *args, **kw)
    return wrapper


def call_later(delay, function, *args, **kw):
    """reactor.callLater, safe from any thread; the IDelayedCall when called in the reactor thread."""
    if isInIOThread():
        return reactor.callLater(delay, function, *args, **kw)
    reactor.callFromThread(reactor.callLater, delay, function, *args, **kw)
    return None


class Timer(object):
    """A restartable single shot timer, in the reactor thread."""

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self._call = None

    @property
    def active(self):
        return self._call is not None and self._call.active()

    def start(self, interval=None):
        self.stop()
        self._call = reactor.callLater(self.interval if interval is None else interval, self._fire)

    def stop(self):
        if self._call is not None and self._call.active():
            self._call.cancel()
        self._call = None

    def _fire(self):
        self._call = None
        self.function()


class RepeatingTimer(Timer):
    def _fire(self):
        self._call = reactor.callLater(self.interval, self._fire)
        self.function()
