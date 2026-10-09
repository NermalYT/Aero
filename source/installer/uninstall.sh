#!/bin/sh
# Removes Aero from Linux or macOS: the app, its Python environment, llama.cpp, the app-menu entry or Aero.app and
# the `aero` command. Your models and data (chats, settings, memory, mods) stay unless you say otherwise.
#
#   sh uninstall.sh            asks whether to delete models and data too
#   sh uninstall.sh --all      deletes everything, including models, data and Aero's saved keys
#   sh uninstall.sh --keep     keeps models and data without asking
#   --dir PATH                 the install folder (default: the folder this script is in)

ALL=""; KEEP=""; DEST=""
while [ $# -gt 0 ]; do
    case "$1" in
        --all) ALL=1 ;;
        --keep) KEEP=1 ;;
        --dir) DEST="$2"; shift ;;
        *) printf 'unknown option %s\n' "$1" ;;
    esac
    shift
done
[ -z "$DEST" ] && DEST=$(cd "$(dirname "$0")" && pwd)
if [ ! -d "$DEST/app/aero" ] && [ ! -d "$DEST/venv" ]; then
    printf '  %s does not look like an Aero install (no app or venv folder).\n' "$DEST"
    exit 1
fi
case "$DEST" in /|"$HOME") printf '  Refusing to remove %s.\n' "$DEST"; exit 1 ;; esac

printf '\n  Uninstalling Aero from %s\n' "$DEST"
if command -v curl >/dev/null 2>&1; then
    curl -fs --noproxy '*' --max-time 3 -X POST "http://127.0.0.1:${AERO_PORT:-8180}/api/shutdown" >/dev/null 2>&1 && sleep 2
fi
if command -v pkill >/dev/null 2>&1; then
    pkill -f "$DEST/venv/bin/python -m aero" >/dev/null 2>&1
    pkill -f "$DEST/llama/" >/dev/null 2>&1
fi

if [ -z "$ALL" ] && [ -z "$KEEP" ] && [ -t 0 ]; then
    printf '  Also delete your models (%s) and data (chats, settings, memory, mods)? [y/N] ' "$DEST/models"
    read -r ans
    case "$ans" in y|Y|yes|YES) ALL=1 ;; esac
fi

for d in app app.new app.old venv llama llama.new llama.old python tools bin; do
    rm -rf "${DEST:?}/$d"
done
rm -f "$DEST/install.sh" "$DEST/README.md" "$DEST/aero.png"

apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
rm -f "$apps/aero.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps" >/dev/null 2>&1
link="$HOME/.local/bin/aero"
if [ -L "$link" ] && case "$(readlink "$link")" in "$DEST"/*) true ;; *) false ;; esac; then rm -f "$link"; fi
[ "$(uname -s)" = Darwin ] && rm -rf "$HOME/Applications/Aero.app"

if [ -n "$ALL" ]; then
    if [ "$(uname -s)" = Darwin ]; then
        while security delete-generic-password -s Aero >/dev/null 2>&1; do :; done        # API keys and tokens
    elif command -v secret-tool >/dev/null 2>&1; then
        secret-tool clear application aero >/dev/null 2>&1
    fi
    rm -rf "${DEST:?}/data" "${DEST:?}/models"
    rm -f "$DEST/uninstall.sh"
    rmdir "$DEST" 2>/dev/null
    printf '  Aero, its models and its data are removed.\n\n'
else
    rm -f "$DEST/uninstall.sh"
    printf '  Aero is removed. Your models and data are still in %s\n' "$DEST"
    printf '  (installing Aero again into the same folder picks them up).\n\n'
fi
