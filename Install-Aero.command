#!/bin/sh
# macOS: double-click to install or update Aero (the first time, right-click it and choose Open: macOS asks
# before running a script that came from the internet). It runs install.sh from this folder in Terminal.
cd "$(dirname "$0")" || exit 1
sh ./install.sh "$@"
status=$?
printf '\n  Press Return to close this window. '
read -r _
exit $status
