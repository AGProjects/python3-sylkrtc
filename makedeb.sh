#!/bin/bash

# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

set -e

distro="${1:-}"

rm -rf dist build *.egg-info

# the source distribution with setuptools itself (the PyPA build module is not needed)
python3 -c "from setuptools import build_meta; build_meta.build_sdist('dist')"
rm -rf *.egg-info      # left in the tree by setuptools
cd dist

tar zxf *.tar.gz
cd */

# add distro suffix to the changelog version (like autopackager does),
# only in the extracted build tree - never in the source repo
if [ -n "$distro" ] && [ "$distro" != "sid" ]; then
    sed -i "s/) unstable/$distro) $distro/" debian/changelog
    head -1 debian/changelog
fi

debuild --no-sign
