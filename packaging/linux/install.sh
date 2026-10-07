#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination="${XDG_DATA_HOME:-$HOME/.local/share}/radmin-compatible-viewer"
if [ -e "$destination" ]; then
    printf '%s\n' "Destination already exists: $destination. Move or remove the old installation first." >&2
    exit 1
fi
mkdir -p "$(dirname -- "$destination")"
cp -a "$here/radmin-compatible-viewer" "$destination"
"$destination/radmin-compatible-viewer" --install-desktop-entry
printf '%s\n' "Installed to $destination" "To uninstall, remove that directory and the radmin-compatible-viewer desktop entry/icon from your XDG data directory. User settings are retained."
