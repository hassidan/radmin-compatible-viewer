#!/bin/sh
set -eu
export QT_QPA_PLATFORM=offscreen
export HOME=/tmp/test-home
export XDG_DATA_HOME="$HOME/data with spaces"
export XDG_CONFIG_HOME="$HOME/config"
mkdir -p "$HOME"
test ! -e "$XDG_CONFIG_HOME"
radmin-compatible-viewer --version
radmin-compatible-viewer --smoke-test --smoke-report /tmp/deb-smoke.json
test -s /tmp/deb-smoke.json
test ! -e "$XDG_CONFIG_HOME"
test -f /usr/share/applications/radmin-compatible-viewer.desktop
test -f /opt/radmin-compatible-viewer/_internal/licenses/manifest.json
dpkg --remove radmin-compatible-viewer
test ! -e /usr/bin/radmin-compatible-viewer
test ! -e /opt/radmin-compatible-viewer
mkdir /tmp/portable
tar -xzf /tmp/portable.tar.gz -C /tmp/portable
"/tmp/portable/radmin-compatible-viewer-0.1.0-linux-x86_64/install.sh"
"$XDG_DATA_HOME/radmin-compatible-viewer/radmin-compatible-viewer" --smoke-test --smoke-report /tmp/portable-smoke.json
test -s /tmp/portable-smoke.json
test -f "$XDG_DATA_HOME/applications/radmin-compatible-viewer.desktop"
test ! -e "$XDG_CONFIG_HOME"
printf '%s\n' 'PASS: Debian 12 clean install, version, frozen smoke, removal, portable per-user install (spaces), portable smoke; network disabled.'
