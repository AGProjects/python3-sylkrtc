#!/usr/bin/python3

# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

import re

from setuptools import setup


def version():
    with open('sylkrtc/__init__.py', encoding='utf-8') as file:
        return re.search(r"^__version__ = '([^']+)'", file.read(), re.M).group(1)


setup(
    name='python3-sylkrtc',
    version=version(),

    description='SylkServer WebRTC gateway client library and tools: video rooms, calls, addressbook',
    long_description=open('README', encoding='utf-8').read(),
    long_description_content_type='text/plain',
    url='https://github.com/AGProjects/python3-sylkrtc',

    author='AG Projects',
    author_email='support@ag-projects.com',
    license='GPL-3.0-or-later',

    platforms=['Platform Independent'],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python',
        'Topic :: Communications :: Conferencing',
    ],

    packages=['sylkrtc'],
    scripts=['sylk-account', 'sylk-addressbook', 'sylk-conference', 'sylk-journal', 'sylk-session', 'sylk-settings'],
    install_requires=['python3-application', 'twisted', 'autobahn', 'dnspython', 'requests', 'zope.interface'],
)
