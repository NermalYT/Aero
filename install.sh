#!/bin/sh
# Aero installer and updater for Linux (every distro family) and macOS.
#
#   sh install.sh               install Aero, or update an existing install, from this folder
#   curl -fsSL https://github.com/NermalYT/Aero/releases/latest/download/install.sh | sh
#                               download the newest release, then install it
#
# Options
#   --dir PATH       install folder (default: ~/.local/share/aero on Linux, ~/Library/Application Support/Aero on macOS)
#   --yes            no questions: install missing system packages, keep (or pick the recommended) router model
#   --update         what Aero's own updater passes: no model chooser, the open window reconnects afterwards
#   --skip-models    don't run the model chooser
#   --skip-llama     leave llama.cpp as it is
#   --build-llama    compile llama.cpp on this machine instead of downloading a prebuilt one
#   --no-launch      don't start Aero at the end
#   --no-shortcuts   no app-menu entry, Aero.app or `aero` command
#   --headless       afterwards, start Aero without a window (the updater passes it when Aero ran with --no-window)
#   --release TAG    with the download form: install this release (e.g. v1.0.0) instead of the newest
#
# Aero installs for your user account and never needs to run as root. It only asks for sudo to install missing
# system packages (Python, the Vulkan loader, a compiler), and shows the command before it runs.
# Models, chats, memory, settings, mods and tunings are never deleted by an update.

REPO="NermalYT/Aero"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n\033[1;36m[%s]\033[0m %s\n' "$1" "$2"; }
warn() { printf '  \033[33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n  \033[31mError:\033[0m %s\n\n  The install did not finish. Models, chats and settings are untouched.\n' "$*" >&2; cleanup; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
cleanup() {
    [ -n "${AERO_SELF_COPY:-}" ] && rm -f "$AERO_SELF_COPY"
    [ -n "${TMPD:-}" ] && [ -d "$TMPD" ] && rm -rf "$TMPD"
    [ -n "${AERO_DL_DIR:-}" ] && [ -d "$AERO_DL_DIR" ] && rm -rf "$AERO_DL_DIR"
    return 0
}

# Run from a copy of this script: the install replaces the original, and sh reads scripts as it goes.
if [ -z "${AERO_SELF_COPY:-}" ] && [ -f "$0" ] && [ "$(basename "$0")" = "install.sh" ]; then
    _here=$(cd "$(dirname "$0")" && pwd)
    _copy=$(mktemp "${TMPDIR:-/tmp}/aero-install.XXXXXX") || exit 1
    cp "$0" "$_copy" || exit 1
    AERO_SELF_COPY="$_copy" AERO_SRC_DIR="$_here" exec sh "$_copy" "$@"
fi

DEST="${AERO_HOME:-}"; YES=""; UPDATE=""; SKIP_MODELS=""; SKIP_LLAMA=""; BUILD_LLAMA=""; NO_LAUNCH=""; NO_SHORTCUTS=""
RELEASE=""; HEADLESS=""
parse_args() {   # a function, so the script's own "$@" stays intact for handing over to a downloaded installer
    while [ $# -gt 0 ]; do
        case "$1" in
            --dir) DEST="$2"; shift ;;
            --dir=*) DEST="${1#--dir=}" ;;
            --yes|-y) YES=1 ;;
            --update) UPDATE=1; YES=1 ;;
            --skip-models) SKIP_MODELS=1 ;;
            --skip-llama) SKIP_LLAMA=1 ;;
            --build-llama) BUILD_LLAMA=1 ;;
            --no-launch) NO_LAUNCH=1 ;;
            --no-shortcuts) NO_SHORTCUTS=1 ;;
            --headless) HEADLESS=1 ;;
            --release) RELEASE="$2"; shift ;;
            -h|--help) sed -n '2,22p' "${AERO_SELF_COPY:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) warn "unknown option $1" ;;
        esac
        shift
    done
}
parse_args "$@"

# ---- what is this machine? ---------------------------------------------------------------------------------------
OS=$(uname -s); ARCH=$(uname -m)
case "$OS" in
    Linux) KIND=linux ;;
    Darwin) KIND=macos ;;
    *) die "Aero's install script runs on Linux and macOS. On Windows, run Update-Aero.bat from Aero-windows.zip." ;;
esac
DISTRO="macOS"; DISTRO_ID=""
if [ "$KIND" = linux ] && [ -r /etc/os-release ]; then
    DISTRO=$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-${NAME:-Linux}}")
    DISTRO_ID=$(. /etc/os-release && printf '%s' "${ID:-}")
elif [ "$KIND" = macos ]; then
    DISTRO="macOS $(sw_vers -productVersion 2>/dev/null)"
fi
[ -z "$DEST" ] && if [ "$KIND" = macos ]; then DEST="$HOME/Library/Application Support/Aero"; else DEST="${XDG_DATA_HOME:-$HOME/.local/share}/aero"; fi
case "$DEST" in /*) ;; *) DEST="$(pwd)/$DEST" ;; esac
[ "$DEST" = "/" ] || [ "$DEST" = "$HOME" ] && die "refusing to install straight into $DEST; pick a folder with --dir"

if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ]; then
    die "run this without sudo: Aero installs for your own account ($SUDO_USER) and asks for sudo itself when it needs a system package."
fi

PM=""
for p in apt-get dnf yum zypper pacman apk xbps-install eopkg emerge swupd brew; do
    if have "$p"; then PM="$p"; break; fi
done
[ "$KIND" = macos ] && { have brew && PM=brew || PM=""; }
IMMUTABLE=""
{ [ -e /run/ostree-booted ] || [ "$DISTRO_ID" = steamos ] || [ "$DISTRO_ID" = nixos ]; } && IMMUTABLE=1
MUSL=""
if [ "$KIND" = linux ] && { ls /lib/ld-musl-* >/dev/null 2>&1 || ldd --version 2>&1 | grep -qi musl; }; then MUSL=1; fi
if [ "$(id -u)" = 0 ]; then SUDO=""; elif have sudo; then SUDO="sudo"; elif have doas; then SUDO="doas"; else SUDO="none"; fi
# started by Aero's own updater: no terminal to type a password into, so sudo must not wait for one
[ -n "$UPDATE" ] && [ ! -t 0 ] && [ "$SUDO" = sudo ] && SUDO="sudo -n"

TMPD=$(mktemp -d "${TMPDIR:-/tmp}/aero.XXXXXX") || die "can't create a temporary folder"
trap cleanup EXIT INT TERM

fetch() {   # fetch URL FILE
    if have curl; then curl -fL --retry 3 --connect-timeout 20 -o "$2" "$1"
    elif have wget; then wget -q -O "$2" "$1"
    else return 127; fi
}
local_get() {   # GET http://127.0.0.1... without any proxy
    if have curl; then curl -fs --noproxy '*' --max-time 3 "$1" >/dev/null 2>&1
    elif have wget; then wget -q --no-proxy -T 3 -O /dev/null "$1"
    else return 1; fi
}
local_post() {
    if have curl; then curl -fs --noproxy '*' --max-time 3 -X POST "$1" >/dev/null 2>&1
    elif have wget; then wget -q --no-proxy -T 3 -O /dev/null --post-data= "$1"
    else return 1; fi
}

# ---- system packages ---------------------------------------------------------------------------------------------
# Logical groups: tools (curl, tar, certificates) · python (3.10+ with venv and pip) · pybuild (to compile a Python
# package that has no wheel for this system) · openmp (the OpenMP runtime prebuilt llama.cpp links against) ·
# vulkan (loader + Mesa drivers) · build (to compile llama.cpp) ·
# vkbuild (to compile llama.cpp's Vulkan backend) · desktop (xdg-utils, clipboard)
pkgs_for() {
    g="$1"
    case "$PM" in
        apt-get) case "$g" in
            tools) echo "curl ca-certificates tar" ;;
            python) echo "python3 python3-venv python3-pip" ;;
            pybuild) echo "build-essential python3-dev" ;;
            openmp) echo "libgomp1" ;;
            vulkan) echo "libvulkan1 mesa-vulkan-drivers" ;;
            build) echo "build-essential cmake ninja-build git" ;;
            vkbuild) echo "glslc libvulkan-dev" ;;
            desktop) echo "xdg-utils xclip" ;; esac ;;
        dnf|yum) case "$g" in
            tools) echo "curl ca-certificates tar" ;;
            python) echo "python3 python3-pip" ;;
            pybuild) echo "gcc gcc-c++ make python3-devel" ;;
            openmp) echo "libgomp" ;;
            vulkan) echo "vulkan-loader mesa-vulkan-drivers" ;;
            build) echo "gcc-c++ make cmake ninja-build git" ;;
            vkbuild) echo "glslc vulkan-headers vulkan-loader-devel" ;;
            desktop) echo "xdg-utils xclip" ;; esac ;;
        zypper) case "$g" in
            tools) echo "curl ca-certificates tar gzip" ;;
            python) echo "python3 python3-pip" ;;
            pybuild) echo "gcc gcc-c++ make python3-devel" ;;
            openmp) echo "libgomp1" ;;
            vulkan) echo "libvulkan1 libvulkan_radeon libvulkan_intel" ;;
            build) echo "gcc-c++ make cmake ninja git" ;;
            vkbuild) echo "shaderc vulkan-devel" ;;
            desktop) echo "xdg-utils xclip" ;; esac ;;
        pacman) case "$g" in
            tools) echo "curl ca-certificates tar" ;;
            python) echo "python python-pip" ;;
            pybuild) echo "base-devel" ;;
            openmp) echo "gcc-libs" ;;
            vulkan) echo "vulkan-icd-loader vulkan-radeon vulkan-intel" ;;
            build) echo "base-devel cmake ninja git" ;;
            vkbuild) echo "shaderc vulkan-headers" ;;
            desktop) echo "xdg-utils xclip" ;; esac ;;
        apk) case "$g" in
            tools) echo "curl ca-certificates tar" ;;
            python) echo "python3 py3-pip" ;;
            pybuild) echo "build-base python3-dev linux-headers" ;;
            openmp) echo "libgomp" ;;
            vulkan) echo "vulkan-loader mesa-vulkan-ati mesa-vulkan-intel" ;;
            build) echo "build-base cmake samurai git linux-headers" ;;
            vkbuild) echo "shaderc vulkan-headers vulkan-loader-dev" ;;
            desktop) echo "xdg-utils xclip" ;; esac ;;
        xbps-install) case "$g" in
            tools) echo "curl ca-certificates tar" ;;
            python) echo "python3 python3-pip" ;;
            pybuild) echo "base-devel python3-devel" ;;
            openmp) echo "libgomp" ;;
            vulkan) echo "vulkan-loader mesa-vulkan-radeon mesa-vulkan-intel" ;;
            build) echo "base-devel cmake ninja git" ;;
            vkbuild) echo "shaderc Vulkan-Headers" ;;
            desktop) echo "xdg-utils xclip" ;; esac ;;
        eopkg) case "$g" in
            tools) echo "curl ca-certs tar" ;;
            python) echo "python3" ;;
            pybuild|build) echo "-c system.devel cmake ninja git" ;;
            vulkan) echo "vulkan" ;;
            vkbuild) echo "vulkan-headers shaderc" ;;
            desktop) echo "xdg-utils xclip" ;; esac ;;
        emerge) case "$g" in
            tools) echo "net-misc/curl app-arch/tar" ;;
            python) echo "dev-lang/python" ;;
            vulkan) echo "media-libs/vulkan-loader" ;;
            build) echo "dev-build/cmake dev-build/ninja dev-vcs/git" ;;
            vkbuild) echo "media-libs/shaderc dev-util/vulkan-headers" ;;
            desktop) echo "x11-misc/xdg-utils x11-misc/xclip" ;; esac ;;
        swupd) case "$g" in
            tools) echo "curl" ;;
            python) echo "python3-basic" ;;
            pybuild|build) echo "c-basic devpkg-vulkan-loader" ;;
            vulkan) echo "vulkan-loader" ;; esac ;;
        brew) case "$g" in
            python) echo "python@3.12" ;;
            build) echo "cmake ninja" ;; esac ;;
    esac
}

pm_install() {   # pm_install PACKAGES...
    case "$PM" in
        apt-get) $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" ||
                 { $SUDO apt-get update -q && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"; } ;;
        dnf) $SUDO dnf install -y "$@" ;;
        yum) $SUDO yum install -y "$@" ;;
        zypper) $SUDO zypper --non-interactive install --no-recommends "$@" ;;
        pacman) $SUDO pacman -S --needed --noconfirm "$@" || $SUDO pacman -Sy --needed --noconfirm "$@" ;;
        apk) $SUDO apk add --no-cache "$@" ;;
        xbps-install) $SUDO xbps-install -Sy "$@" ;;
        eopkg) $SUDO eopkg install -y "$@" ;;
        emerge) $SUDO emerge --noreplace --quiet "$@" ;;
        swupd) $SUDO swupd bundle-add "$@" ;;
        brew) brew install "$@" ;;
        *) return 1 ;;
    esac
}

install_group() {   # install_group GROUP [why]: installs a group of system packages, asking first unless --yes
    pk=$(pkgs_for "$1")
    [ -z "$pk" ] && return 1
    if [ -n "$IMMUTABLE" ]; then
        warn "$DISTRO has a read-only system, so Aero won't install packages ($pk) into it."
        return 1
    fi
    if [ "$SUDO" = none ]; then
        warn "Installing $pk needs root, and neither sudo nor doas is available."
        return 1
    fi
    say "${2:-Aero needs these system packages}: $pk"
    say "Command: ${SUDO:+$SUDO }$PM install $pk"
    if [ -z "$YES" ] && [ -t 0 ]; then
        printf '  Install them now? [Y/n] '
        read -r ans </dev/tty || ans=y
        case "$ans" in n|N|no|NO) return 1 ;; esac
    fi
    # shellcheck disable=SC2086
    pm_install $pk
}

# ---- where does the new Aero come from? -------------------------------------------------------------------------
SRC="${AERO_SRC_DIR:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC/source/aero/__main__.py" ]; then
    # piped from curl, or a lone install.sh: download the release archive and run the install.sh inside it
    have curl || have wget || install_group tools "Downloading Aero needs curl or wget" || die "install curl or wget, then run this again"
    asset="Aero-linux.tar.gz"; [ "$KIND" = macos ] && asset="Aero-macos.zip"
    if [ -n "$RELEASE" ]; then url="https://github.com/$REPO/releases/download/$RELEASE/$asset"
    else url="https://github.com/$REPO/releases/latest/download/$asset"; fi
    step "Aero" "Downloading $url"
    fetch "$url" "$TMPD/$asset" || die "download failed: $url"
    mkdir -p "$TMPD/rel"
    case "$asset" in
        *.zip) (cd "$TMPD/rel" && unzip -q "../$asset") || die "couldn't unpack $asset" ;;
        *) tar -xzf "$TMPD/$asset" -C "$TMPD/rel" || die "couldn't unpack $asset" ;;
    esac
    inner="$TMPD/rel/Aero/install.sh"
    [ -f "$inner" ] && [ -f "$TMPD/rel/Aero/source/aero/__main__.py" ] || die "$asset doesn't contain Aero"
    dl="$TMPD"; TMPD=""; rm -f "${AERO_SELF_COPY:-/nonexistent}"
    # the downloaded installer takes over (and deletes the download when it is done); give it the terminal,
    # since stdin is this script itself when it was piped from curl
    if [ ! -t 0 ] && (exec </dev/tty) 2>/dev/null; then AERO_SELF_COPY="" AERO_SRC_DIR="" AERO_DL_DIR="$dl" exec sh "$inner" "$@" </dev/tty
    else AERO_SELF_COPY="" AERO_SRC_DIR="" AERO_DL_DIR="$dl" exec sh "$inner" "$@"; fi
fi
NEWVER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/source/aero/config.py" | head -n 1)
OLDVER=""
[ -f "$DEST/app/aero/config.py" ] && OLDVER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/app/aero/config.py" | head -n 1)

printf '\n  ==============================================================\n'
printf '    Aero %s installer  ·  %s (%s)\n' "$NEWVER" "$DISTRO" "$ARCH"
printf '    Install folder: %s\n' "$DEST"
[ -n "$OLDVER" ] && printf '    Updating from Aero %s\n' "$OLDVER"
printf '  ==============================================================\n'

# ---- 1. stop a running Aero ----------------------------------------------------------------------------------------
step "1/7" "Stopping Aero if it is running"
PORT="${AERO_PORT:-8180}"
if local_get "http://127.0.0.1:$PORT/api/state"; then
    [ -z "$UPDATE" ] && local_post "http://127.0.0.1:$PORT/api/shutdown"
    n=0
    while local_get "http://127.0.0.1:$PORT/api/state" && [ $n -lt 40 ]; do sleep 0.5; n=$((n + 1)); done
fi
if have pkill; then
    pkill -f "$DEST/venv/bin/python -m aero" >/dev/null 2>&1
    pkill -f "$DEST/llama/" >/dev/null 2>&1
fi
say "Done."

# ---- 2. Python 3.10+ -----------------------------------------------------------------------------------------------
step "2/7" "Looking for Python 3.10 or newer"
have curl || have wget || install_group tools || warn "neither curl nor wget is installed"
py_ok() { "$1" -c 'import sys, venv; sys.exit(0 if (3, 10) <= sys.version_info[:2] else 1)' >/dev/null 2>&1; }
find_python() {
    for c in python3.12 python3.13 python3.11 python3.14 python3.10 python3 \
             /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3.12 /usr/local/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.13/bin/python3; do
        p=$(command -v "$c" 2>/dev/null) || continue
        if py_ok "$p"; then echo "$p"; return 0; fi
    done
    return 1
}
UV=""
get_uv() {   # a private copy of uv (Astral's Python manager), used when the system has no usable Python 3.10+
    [ -n "$UV" ] && return 0
    case "$KIND-$ARCH" in
        linux-x86_64) t=x86_64-unknown-linux ;;
        linux-aarch64|linux-arm64) t=aarch64-unknown-linux ;;
        macos-arm64) t=aarch64-apple-darwin ;;
        macos-x86_64) t=x86_64-apple-darwin ;;
        *) return 1 ;;
    esac
    [ "$KIND" = linux ] && { [ -n "$MUSL" ] && t="$t-musl" || t="$t-gnu"; }
    say "Fetching uv to set up a private Python 3.12 for Aero" >&2     # stderr: callers may capture stdout
    fetch "https://github.com/astral-sh/uv/releases/latest/download/uv-$t.tar.gz" "$TMPD/uv.tgz" || return 1
    mkdir -p "$TMPD/uv" && tar -xzf "$TMPD/uv.tgz" -C "$TMPD/uv" || return 1
    mkdir -p "$DEST/tools"
    for f in "$TMPD"/uv/*/uv "$TMPD"/uv/uv; do      # uv-<target>/uv in the archive (no find: minimal images lack it)
        [ -f "$f" ] && cp "$f" "$DEST/tools/uv" && chmod +x "$DEST/tools/uv" && UV="$DEST/tools/uv" && break
    done
    [ -n "$UV" ]
}
uv_python() {
    get_uv || return 1
    UV_PYTHON_INSTALL_DIR="$DEST/python" "$UV" python install 3.12 >/dev/null 2>&1 || return 1
    UV_PYTHON_INSTALL_DIR="$DEST/python" "$UV" python find 3.12 2>/dev/null
}
PY=$(find_python)
if [ -z "$PY" ] && [ "$KIND" = linux ]; then
    install_group python "Aero needs Python 3.10+ with venv and pip" && PY=$(find_python)
    if [ -z "$PY" ]; then        # long-term-support releases whose python3 is too old ship a newer one beside it
        case "$PM" in
            apt-get) pm_install python3.11 python3.11-venv >/dev/null 2>&1 || pm_install python3.12 python3.12-venv >/dev/null 2>&1 ;;
            dnf|yum) pm_install python3.12 python3.12-pip >/dev/null 2>&1 || pm_install python3.11 python3.11-pip >/dev/null 2>&1 ;;
            zypper) pm_install python312 python312-pip >/dev/null 2>&1 || pm_install python311 python311-pip >/dev/null 2>&1 ;;
        esac
        PY=$(find_python)
    fi
elif [ -z "$PY" ] && [ "$PM" = brew ]; then
    install_group python && PY=$(find_python)
fi
if [ -z "$PY" ]; then
    say "No usable Python found; installing a private Python 3.12 inside $DEST/python."
    if get_uv; then PY=$(uv_python) || PY=""; fi          # get_uv here, not in $(...), so UV stays set
fi
[ -z "$PY" ] && die "couldn't get Python 3.10+. Install Python 3.12 with your package manager (or from python.org on macOS), then run this again."
say "Using $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"
if [ "$DISTRO_ID" = nixos ]; then
    warn "On NixOS, prebuilt Python wheels and llama.cpp need programs.nix-ld enabled (or run this inside nix-shell -p python312 cmake gcc)."
fi

# ---- 3. the app files ------------------------------------------------------------------------------------------------
step "3/7" "Copying Aero $NEWVER to $DEST/app"
mkdir -p "$DEST/data" "$DEST/models" || die "can't create $DEST"
rm -rf "$DEST/app.new"
cp -R "$SRC/source" "$DEST/app.new" || die "copying the app failed"
find "$DEST/app.new" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
if [ -d "$DEST/app" ]; then rm -rf "$DEST/app.old"; mv "$DEST/app" "$DEST/app.old" || die "can't replace $DEST/app (is a file in it open?)"; fi
mv "$DEST/app.new" "$DEST/app" || die "can't move the new app into place"
rm -rf "$DEST/app.old"
cp "$SRC/install.sh" "$DEST/install.sh" 2>/dev/null && chmod +x "$DEST/install.sh"
cp "$DEST/app/installer/uninstall.sh" "$DEST/uninstall.sh" 2>/dev/null && chmod +x "$DEST/uninstall.sh"
[ -f "$SRC/source/README.md" ] && cp "$SRC/source/README.md" "$DEST/README.md"
say "Done."

# ---- 4. Aero's own Python environment ------------------------------------------------------------------------------
step "4/7" "Setting up Aero's Python environment (the first run takes a few minutes)"
VPY="$DEST/venv/bin/python"
if [ -x "$VPY" ] && ! py_ok "$VPY"; then rm -rf "$DEST/venv"; fi
if [ ! -x "$VPY" ]; then
    if ! "$PY" -m venv "$DEST/venv" >"$TMPD/venv.log" 2>&1; then
        rm -rf "$DEST/venv"
        if [ "$PM" = apt-get ]; then          # Debian and Ubuntu split venv/ensurepip into python3.X-venv
            pv=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
            pm_install "python$pv-venv" >/dev/null 2>&1 || install_group python
        fi
        "$PY" -m venv "$DEST/venv" >"$TMPD/venv.log" 2>&1 || {
            rm -rf "$DEST/venv"
            get_uv && "$UV" venv --seed --python "$PY" "$DEST/venv" >"$TMPD/venv.log" 2>&1
        } || die "couldn't create the Python environment: $(tail -n 3 "$TMPD/venv.log")"
    fi
fi
"$VPY" -m pip --version >/dev/null 2>&1 || "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 ||
    die "the Python environment has no pip; install your distro's python3-pip package and run this again"
"$VPY" -m pip install --upgrade pip --disable-pip-version-check -q </dev/null >/dev/null 2>&1
pip_core() { "$VPY" -m pip install --upgrade -r "$DEST/app/requirements.txt" --disable-pip-version-check -q </dev/null; }
if ! pip_core; then
    say "A package had to be compiled for this system; adding a compiler and Python headers, then trying again."
    { install_group pybuild "Compiling Python packages needs" && pip_core; } || die "installing Aero's Python packages failed (see above)"
fi
if [ -f "$DEST/app/requirements-extra.txt" ]; then
    while IFS= read -r req; do
        case "$req" in ''|'#'*) continue ;; esac
        "$VPY" -m pip install --upgrade "$req" --disable-pip-version-check -q </dev/null >"$TMPD/pip.log" 2>&1 ||
            warn "optional package $req isn't available for this system ($(tail -n 1 "$TMPD/pip.log")); Aero works without it."
    done < "$DEST/app/requirements-extra.txt"
fi
say "Done."

# ---- 5. llama.cpp, icon, shortcuts ------------------------------------------------------------------------------------
step "5/7" "Installing llama.cpp for this machine's GPU and creating shortcuts"
SETUP_ARGS=""
[ -n "$SKIP_LLAMA" ] && SETUP_ARGS="$SETUP_ARGS --skip-llama"
[ -n "$BUILD_LLAMA" ] && SETUP_ARGS="$SETUP_ARGS --build-llama"
[ -n "$NO_SHORTCUTS" ] && SETUP_ARGS="$SETUP_ARGS --no-shortcuts"
run_setup() {
    # shellcheck disable=SC2086
    "$VPY" "$DEST/app/installer/setup.py" --dest "$DEST" $SETUP_ARGS </dev/null
}
run_setup; rc=$?
tries=0; handled=" "
while [ "$rc" = 3 ] && [ $tries -lt 4 ]; do        # setup.py exits 3 when llama.cpp needs a system package
    tries=$((tries + 1))
    need=$(cat "$DEST/data/setup-needs.txt" 2>/dev/null)
    again=""; case "$handled" in *" $need "*) again=1 ;; esac
    handled="$handled$need "
    case "$need" in
        vulkan)
            if [ -n "$again" ] || ! install_group vulkan "llama.cpp's Vulkan build needs the Vulkan loader and driver"; then
                SETUP_ARGS="$SETUP_ARGS --no-vulkan"
            fi ;;
        openmp)
            if [ -n "$again" ] || ! install_group openmp "The prebuilt llama.cpp needs the OpenMP runtime"; then
                SETUP_ARGS="$SETUP_ARGS --no-openmp"
            fi ;;
        build-tools)
            [ -z "$again" ] || break
            install_group build "Compiling llama.cpp for this system needs" || die "llama.cpp can't be built without a compiler and CMake"
            if [ -e /dev/dri ] || have nvidia-smi; then install_group vkbuild "GPU support in the build needs" || true; fi
            SETUP_ARGS="$SETUP_ARGS --build-llama" ;;
        *) break ;;
    esac
    run_setup; rc=$?
done
[ "$rc" = 0 ] || die "setting up llama.cpp failed (see above). Aero's app files are installed; run this again to retry."
if [ "$KIND" = linux ] && ! have xdg-open && [ -z "$IMMUTABLE" ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    install_group desktop "Opening files and links from Aero uses" || true
fi

# ---- 6. settings, then the model chooser ------------------------------------------------------------------------------
step "6/7" "Settings and models"
cd "$DEST/app" || die "can't enter $DEST/app"
AERO_HOME="$DEST" "$VPY" -m aero.migrate --settings-only </dev/null
if [ -n "$SKIP_MODELS" ] || [ -n "$UPDATE" ]; then
    say "Skipped the model chooser. Download models from Aero's model screen."
elif [ -n "$YES" ] || [ ! -t 0 ]; then
    AERO_HOME="$DEST" "$VPY" -m aero.setup_models --yes </dev/null
else
    AERO_HOME="$DEST" "$VPY" -m aero.setup_models
fi

# ---- 7. done ----------------------------------------------------------------------------------------------------------
step "7/7" "Done"
say "Aero $NEWVER is installed in $DEST"
if [ "$KIND" = macos ]; then say "Open it with Aero in ~/Applications (Launchpad, Spotlight), or type: aero"
else say "Open it from your app menu (Aero), or type: aero"; fi
say "Update later with: sh \"$DEST/install.sh\"   (Aero also offers updates itself when it starts)"
say "Uninstall with:    sh \"$DEST/uninstall.sh\""
if [ -z "$NO_LAUNCH" ]; then
    if [ -n "$HEADLESS" ]; then
        say "Starting Aero without a window (it was running that way before the update)..."
        (cd "$DEST/app" && AERO_HOME="$DEST" nohup "$VPY" -m aero --no-window --reopen >/dev/null 2>&1 &)
    elif [ "$KIND" = linux ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        say "No desktop session here: run  aero --no-window  and open http://127.0.0.1:$PORT in a browser"
        say "(from another computer: ssh -L $PORT:127.0.0.1:$PORT this-machine)."
    else
        say "Starting Aero..."
        REOPEN=""; [ -n "$UPDATE" ] && REOPEN="--reopen"
        # shellcheck disable=SC2086
        (cd "$DEST/app" && AERO_HOME="$DEST" nohup "$VPY" -m aero $REOPEN >/dev/null 2>&1 &)
    fi
fi
cleanup
exit 0
