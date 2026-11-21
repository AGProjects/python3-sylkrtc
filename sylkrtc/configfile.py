# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""The SIP SIMPLE SDK's configuration file format, without the SDK.

The same parser and writer as sipsimple.configuration.backend.file.FileBackend, so a
file goes back exactly as the SDK writes it: the tools that only read accounts or keep
the device id (sylk-account, sylk-journal, sylk-addressbook, sylk-session,
sylk-conference) need no SIP stack, which matters where python3-sipsimple is not
packaged (macOS). sylk-settings still writes with the SDK's configuration manager.
"""

import errno
import os
import random
import re

from collections import deque


__all__ = ['ConfigFileError', 'ConfigFile']


class ConfigFileError(Exception):
    pass


class _Group(object):
    def __init__(self, indentation):
        self.indentation = indentation
        self.data = {}


class _Line(object):
    def __init__(self, indentation, name, separator, value):
        self.indentation = indentation
        self.name = name
        self.separator = separator
        self.value = value


class ConfigFile(object):
    """load(): the file as nested dicts of str and lists of str ({} when there is none); save(data): writes it."""

    escape_characters_re = re.compile(r"""[,"'=: #\\\t\x0b\x0c\n\r]""")

    def __init__(self, filename, encoding='utf-8'):
        self.filename = filename
        self.encoding = encoding

    def load(self):
        try:
            file = open(self.filename, 'rb')
        except IOError as e:
            if e.errno == errno.ENOENT:
                return {}
            raise ConfigFileError(f'failed to read configuration file: {e}')
        stack = deque([_Group(-1)])
        with file:
            for lineno, line in enumerate(file, 1):
                line = self._parse_line(line.decode(self.encoding), lineno)
                if not line.name:
                    continue
                while stack[0].indentation >= line.indentation:
                    stack.popleft()
                if line.separator == ':':
                    group = _Group(line.indentation)
                    stack[0].data[line.name] = group.data
                    stack.appendleft(group)
                else:
                    stack[0].data[line.name] = line.value
        return stack[-1].data

    def save(self, data):
        lines = self._build_group(data, 0)
        directory = os.path.dirname(self.filename)
        tmp_filename = '%s.%d.%08X' % (self.filename, os.getpid(), random.getrandbits(32))
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd = os.open(tmp_filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as file:
                file.write((os.linesep.join(lines) + os.linesep).encode(self.encoding))
            os.replace(tmp_filename, self.filename)
        except (IOError, OSError) as e:
            try:
                os.unlink(tmp_filename)
            except OSError:
                pass
            raise ConfigFileError(f'failed to write configuration file: {e}')

    def _parse_line(self, line, lineno):
        def advance_to_next_token(line):
            counter = 0
            while line and line[0].isspace():
                line.popleft()
                counter += 1
            if line and line[0] == '#':
                line.clear()
            return counter

        def token_iterator(line, delimiter=''):
            quote_char = None
            while line:
                if quote_char is None and line[0] in delimiter:
                    break
                char = line.popleft()
                if char in "'\"":
                    if quote_char is None:
                        quote_char = char
                        continue
                    elif quote_char == char:
                        quote_char = None
                        continue
                    else:
                        yield char
                elif char == '\\':
                    if not line:
                        raise ConfigFileError(f"unexpected `\\' at end of line {lineno}")
                    char = line.popleft()
                    yield {'n': '\n', 'r': '\r'}.get(char, char)
                elif quote_char is None and char == '#':
                    line.clear()
                    break
                elif quote_char is None and char.isspace():
                    break
                else:
                    yield char
            if quote_char is not None:
                raise ConfigFileError(f'missing ending quote at line {lineno}')

        line = deque(line.rstrip())
        indentation = advance_to_next_token(line)
        if not line:
            return _Line(indentation, None, None, None)
        name = ''.join(token_iterator(line, delimiter=':='))
        advance_to_next_token(line)
        if not line or line[0] not in ':=':
            raise ConfigFileError(f"expected one of `:' or `=' at line {lineno}")
        if not name:
            raise ConfigFileError(f'missing setting/section name at line {lineno}')
        separator = line.popleft()
        advance_to_next_token(line)
        if not line:
            return _Line(indentation, name, separator, None)
        elif separator == ':':
            raise ConfigFileError(f"unexpected characters after `:' at line {lineno}")
        value = None
        value_list = None
        while line:
            value = ''.join(token_iterator(line, delimiter=','))
            advance_to_next_token(line)
            if line:
                if line[0] == ',':
                    line.popleft()
                    advance_to_next_token(line)
                    if value_list is None:
                        value_list = []
                else:
                    raise ConfigFileError(f'unexpected characters after value at line {lineno}')
            if value_list is not None:
                value_list.append(value)
        return _Line(indentation, name, separator, value_list if value_list is not None else value)

    def _build_group(self, group, indentation):
        setting_lines = []
        group_lines = []
        indent = ' ' * 4 * indentation
        for name, data in sorted(group.items()):
            if data is None:
                setting_lines.append(f'{indent}{self._escape(name)} =')
            elif type(data) is dict:
                group_lines.append(f'{indent}{self._escape(name)}:')
                group_lines.extend(self._build_group(data, indentation + 1))
                group_lines.append('')
            elif type(data) is list:
                value = ', '.join(self._escape(item) for item in data)
                if len(data) == 1:
                    value += ','
                setting_lines.append(f'{indent}{self._escape(name)} = {value}')
            elif type(data) is str:
                setting_lines.append(f'{indent}{self._escape(name)} = {self._escape(data)}')
            else:
                raise ConfigFileError(f'expected str, dict or list object, got {type(data).__name__}')
        return setting_lines + group_lines

    def _escape(self, value):
        if value == '':
            return '""'
        elif self.escape_characters_re.search(value):
            return '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        return value
