#!/usr/bin/env bash
# =====================================================================
#  George installer
#
#      curl -fsSL https://raw.githubusercontent.com/the-priest/George/main/install.sh | bash
#
#  Tuned for CachyOS, works on any Linux with a package manager.
#  Installs the GTK4 stack, installs Ollama if it is missing, pulls the
#  model, drops George in ~/.local/share/george with a launcher, a
#  desktop entry and an icon.
#
#  Flags:   --uninstall  --yes  --no-model  --model <tag>  --deps-only
#           --no-deps  --allow-remote-ollama
#  Env:     GEORGE_REPO  GEORGE_BRANCH  GEORGE_MODEL  GEORGE_PREFIX
# =====================================================================
set -euo pipefail

REPO="${GEORGE_REPO:-the-priest/George}"
BRANCH="${GEORGE_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
MODEL="${GEORGE_MODEL:-deepseek-r1:7b}"

PREFIX="${GEORGE_PREFIX:-$HOME/.local}"
APP_DIR="${PREFIX}/share/george"
BIN_DIR="${PREFIX}/bin"
DESKTOP_DIR="${PREFIX}/share/applications"
ICON_DIR="${PREFIX}/share/icons/hicolor/scalable/apps"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/george"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/george"

REQUIRED_FILES=(george.py george_core.py george_tools.py george_voice.py george_theme.py george_hud.py)
OPTIONAL_FILES=(README.md george.svg install.sh)

ASSUME_YES=0
DO_MODEL=1
DO_DEPS=1
ALLOW_REMOTE_OLLAMA=0
DEPS_ONLY=0
DO_UNINSTALL=0

# ---------------------------------------------------------------- ui --
if [ -t 1 ]; then
  C_ACC=$'\033[38;5;45m'; C_OK=$'\033[38;5;42m'; C_WARN=$'\033[38;5;214m'
  C_ERR=$'\033[38;5;203m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_ACC=""; C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

say()  { printf '%s::%s %s\n' "${C_ACC}" "${C_OFF}" "$*"; }
ok()   { printf '%s ok%s %s\n' "${C_OK}" "${C_OFF}" "$*"; }
warn() { printf '%s !!%s %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }
die()  { printf '%s xx%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; exit 1; }
dim()  { printf '%s   %s%s\n' "${C_DIM}" "$*" "${C_OFF}"; }

banner() {
  printf '%s\n' "${C_ACC}"
  cat <<'ART'
   _____ ______ ____  _____   _____ ______
  / ___// __/ // __ \/ __  \ / ___// __/ /
 / (_ // _// _ / /_/ / /_/ // (_ // _//_/
 \___//___/\___\____/_____/ \___//___(_)
ART
  printf '%s' "${C_OFF}"
  dim "local desktop AI  .  no api keys  .  ollama only"
  echo
}

# Prompts have to come from the terminal: when this script is piped from
# curl, stdin IS the script, so a bare `read` eats the rest of it.
#
# `[ -r /dev/tty ]` is not enough. The device node exists and is mode
# readable inside a service, a container or a cron job, but opening it
# fails with ENXIO because there is no controlling terminal -- which
# leaked a raw bash error on the way out of --uninstall. Try to open it
# for real instead of trusting the permission bits.
has_tty() { { : >/dev/tty; } 2>/dev/null; }

ask() {
  local prompt="$1" reply=""
  if [ "${ASSUME_YES}" = "1" ] || ! has_tty; then
    return 0
  fi
  printf '%s?%s %s [Y/n] ' "${C_ACC}" "${C_OFF}" "${prompt}" > /dev/tty 2>/dev/null
  read -r reply < /dev/tty 2>/dev/null || reply=""
  case "${reply}" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# For anything that DELETES. A question with no terminal to answer it is
# not consent, so with no tty this defaults to no -- and --yes does not
# override it either. Losing someone's notes and chats to an unattended
# run is not a trade worth making.
ask_destructive() {
  local prompt="$1" reply=""
  has_tty || { dim "no terminal to ask on - keeping it"; return 1; }
  printf '%s?%s %s [y/N] ' "${C_WARN}" "${C_OFF}" "${prompt}" > /dev/tty 2>/dev/null
  read -r reply < /dev/tty 2>/dev/null || reply=""
  case "${reply}" in [yY]*) return 0 ;; *) return 1 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------- distro ----
DISTRO_ID=""; DISTRO_LIKE=""; DISTRO_NAME=""; PKG=""; SUDO=""

detect_distro() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"; DISTRO_LIKE="${ID_LIKE:-}"
    DISTRO_NAME="${PRETTY_NAME:-${NAME:-linux}}"
  fi
  local family="${DISTRO_ID} ${DISTRO_LIKE}"
  case "${family}" in
    *arch*)   PKG="pacman" ;;
    *debian*|*ubuntu*) PKG="apt" ;;
    *fedora*|*rhel*|*centos*) PKG="dnf" ;;
    *suse*)   PKG="zypper" ;;
    *alpine*) PKG="apk" ;;
    *void*)   PKG="xbps" ;;
  esac
  # ID_LIKE is missing on a few distros; fall back to what is on PATH
  if [ -z "${PKG}" ]; then
    if   have pacman;  then PKG="pacman"
    elif have apt-get; then PKG="apt"
    elif have dnf;     then PKG="dnf"
    elif have zypper;  then PKG="zypper"
    elif have apk;     then PKG="apk"
    elif have xbps-install; then PKG="xbps"
    fi
  fi
  if [ "$(id -u)" != "0" ]; then
    if have sudo; then SUDO="sudo"
    elif have doas; then SUDO="doas"
    fi
  fi
}

pkg_install() {
  [ "$#" -gt 0 ] || return 0
  case "${PKG}" in
    pacman) ${SUDO} pacman -Syu --needed --noconfirm "$@" ;;
    apt)    ${SUDO} apt-get update -qq && ${SUDO} apt-get install -y "$@" ;;
    dnf)    ${SUDO} dnf install -y "$@" ;;
    zypper) ${SUDO} zypper --non-interactive install -y "$@" ;;
    apk)    ${SUDO} apk add "$@" ;;
    xbps)   ${SUDO} xbps-install -Sy "$@" ;;
    *)      return 1 ;;
  esac
}

# Optional extras must never take the whole install down with them, so
# they go in one at a time and a failure is just a warning.
pkg_install_soft() {
  local p
  for p in "$@"; do
    pkg_install "${p}" >/dev/null 2>&1 && dim "extra: ${p}" || true
  done
}

core_packages() {
  case "${PKG}" in
    pacman) echo "python python-gobject python-cairo gtk4 libadwaita curl" ;;
    apt)    echo "python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 curl" ;;
    dnf)    echo "python3 python3-gobject python3-cairo gtk4 libadwaita curl" ;;
    zypper) echo "python3 python3-gobject python3-cairo gtk4 libadwaita curl" ;;
    apk)    echo "python3 py3-gobject3 py3-cairo gtk4.0 libadwaita curl" ;;
    xbps)   echo "python3 python3-gobject python3-cairo gtk4 libadwaita curl" ;;
    *)      echo "" ;;
  esac
}

extra_packages() {
  case "${PKG}" in
    pacman) echo "espeak-ng playerctl wl-clipboard grim libnotify ffmpeg" ;;
    apt)    echo "espeak-ng playerctl wl-clipboard grim libnotify-bin ffmpeg" ;;
    dnf)    echo "espeak-ng playerctl wl-clipboard grim libnotify ffmpeg" ;;
    zypper) echo "espeak-ng playerctl wl-clipboard grim libnotify-tools ffmpeg" ;;
    apk)    echo "espeak-ng playerctl wl-clipboard grim libnotify ffmpeg" ;;
    xbps)   echo "espeak-ng playerctl wl-clipboard grim libnotify ffmpeg" ;;
    *)      echo "" ;;
  esac
}

# ------------------------------------------------------------- gpu ----
gpu_kind() {
  local blob=""
  if have lspci; then
    blob="$(lspci 2>/dev/null || true)"
  fi
  case "${blob}" in
    *NVIDIA*|*nVidia*) echo "nvidia"; return ;;
    *"Advanced Micro Devices"*|*AMD/ATI*) echo "amd"; return ;;
  esac
  if [ -d /sys/module/nvidia ]; then echo "nvidia"; return; fi
  if [ -d /sys/module/amdgpu ]; then echo "amd"; return; fi
  echo "cpu"
}

ollama_package() {
  # Arch (and therefore CachyOS) ships accelerated builds in the repos.
  local gpu; gpu="$(gpu_kind)"
  if [ "${PKG}" = "pacman" ]; then
    case "${gpu}" in
      nvidia) echo "ollama-cuda" ;;
      amd)    echo "ollama-rocm" ;;
      *)      echo "ollama" ;;
    esac
    return
  fi
  case "${PKG}" in
    dnf) echo "ollama" ;;
    *)   echo "" ;;
  esac
}

# --------------------------------------------------------- ollama -----
ollama_running() {
  local url="http://localhost:11434/api/tags"
  curl -fsS --max-time 3 "${url}" >/dev/null 2>&1
}

OLLAMA_TMP_PID=""

ollama_up() {
  ollama_running && return 0
  have ollama || return 1
  say "starting ollama for the pull"
  nohup ollama serve >"${DATA_DIR}/ollama-install.log" 2>&1 &
  OLLAMA_TMP_PID="$!"
  local i=0
  while [ "${i}" -lt 60 ]; do
    ollama_running && { ok "engine up"; return 0; }
    sleep 0.5; i=$((i + 1))
  done
  return 1
}

ollama_down() {
  # Only stop the daemon this script started -- exactly what the app does.
  if [ -n "${OLLAMA_TMP_PID}" ] && kill -0 "${OLLAMA_TMP_PID}" 2>/dev/null; then
    kill "${OLLAMA_TMP_PID}" 2>/dev/null || true
    sleep 1
    kill -9 "${OLLAMA_TMP_PID}" 2>/dev/null || true
    OLLAMA_TMP_PID=""
  fi
}
trap ollama_down EXIT

ensure_ollama() {
  if have ollama; then
    ok "ollama already installed ($(ollama --version 2>/dev/null | head -n1 || echo present))"
    return 0
  fi
  local pkg; pkg="$(ollama_package)"
  if [ -n "${pkg}" ]; then
    say "installing ${pkg} ($(gpu_kind) build)"
    if pkg_install "${pkg}"; then
      ok "ollama installed from your repos"
      return 0
    fi
    warn "${pkg} failed from the repos, falling back"
  fi
  warn "no ollama package for this distro"
  dim "the official installer is a script piped into a shell:"
  dim "  curl -fsSL https://ollama.com/install.sh | sh"
  # --yes covers package installs; it does NOT auto-approve piping a
  # remote script into a shell. That one stays a deliberate keystroke.
  local approve=1
  if [ "${ALLOW_REMOTE_OLLAMA}" = "1" ]; then
    approve=0
  elif [ -r /dev/tty ]; then
    printf '%s?%s run it now? [y/N] ' "${C_ACC}" "${C_OFF}" > /dev/tty
    local r=""; read -r r < /dev/tty || r=""
    case "${r}" in [yY]*) approve=0 ;; esac
  fi
  if [ "${approve}" = "0" ]; then
    curl -fsSL https://ollama.com/install.sh | sh
    have ollama && { ok "ollama installed"; return 0; }
  fi
  warn "skipping ollama - George will tell you it is missing on first run"
  return 1
}

pull_model() {
  [ "${DO_MODEL}" = "1" ] || { dim "skipping the model pull"; return 0; }
  have ollama || return 0
  ollama_up || { warn "could not start ollama; pull ${MODEL} yourself later"; return 0; }
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "${MODEL}"; then
    ok "${MODEL} already pulled"
  else
    say "pulling ${MODEL} (this is the big one - several GB)"
    ollama pull "${MODEL}" || warn "pull failed; you can retry inside George under Models"
  fi
  ollama_down
}

# ---------------------------------------------------------- files -----
SRC_DIR=""
CLEANUP_SRC=""

resolve_source() {
  local self="${BASH_SOURCE[0]:-$0}" here=""
  # Piped from curl, BASH_SOURCE is "bash", not a path -- so a checkout
  # only counts when this script is genuinely a file on disk next to the
  # sources.  Otherwise always fetch, or a stale cwd silently wins.
  if [ -f "${self}" ]; then
    here="$(cd "$(dirname "${self}")" 2>/dev/null && pwd || echo "")"
  fi
  if [ -n "${here}" ] && [ -f "${here}/george.py" ] && [ -f "${here}/george_core.py" ]; then
    SRC_DIR="${here}"
    dim "installing from this checkout: ${SRC_DIR}"
    return
  fi
  say "fetching George from ${REPO}@${BRANCH}"
  SRC_DIR="$(mktemp -d)"
  CLEANUP_SRC="${SRC_DIR}"
  local f
  for f in "${REQUIRED_FILES[@]}"; do
    curl -fsSL "${RAW}/${f}" -o "${SRC_DIR}/${f}" \
      || die "could not fetch ${f} from ${RAW} - is the repo public and the branch right?"
    dim "got ${f}"
  done
  for f in "${OPTIONAL_FILES[@]}"; do
    curl -fsSL "${RAW}/${f}" -o "${SRC_DIR}/${f}" 2>/dev/null || true
  done
}

verify_source() {
  local f
  for f in "${REQUIRED_FILES[@]}"; do
    [ -s "${SRC_DIR}/${f}" ] || die "${f} is missing or empty"
  done
  if have python3; then
    python3 -m py_compile "${SRC_DIR}"/*.py \
      || die "the downloaded files do not compile - refusing to install them"
    ok "source compiles"
  fi
}

install_files() {
  mkdir -p "${APP_DIR}" "${BIN_DIR}" "${DESKTOP_DIR}" "${ICON_DIR}" \
           "${CONFIG_DIR}" "${DATA_DIR}"
  local f
  for f in "${REQUIRED_FILES[@]}"; do
    install -m 0644 "${SRC_DIR}/${f}" "${APP_DIR}/${f}"
  done
  chmod 0755 "${APP_DIR}/george.py"
  for f in "${OPTIONAL_FILES[@]}"; do
    [ -f "${SRC_DIR}/${f}" ] && install -m 0644 "${SRC_DIR}/${f}" "${APP_DIR}/${f}"
  done
  [ -f "${SRC_DIR}/install.sh" ] && \
    install -m 0755 "${SRC_DIR}/install.sh" "${APP_DIR}/install.sh"
  [ -f "${SRC_DIR}/george.svg" ] && \
    install -m 0644 "${SRC_DIR}/george.svg" "${ICON_DIR}/com.thepriest.george.svg"
  # a v1/v2 install wrote org.* -- the ID never matched the app, so the
  # window fell back to the generic python icon. Clear the stale pair.
  rm -f "${DESKTOP_DIR}/org.thepriest.george.desktop" \
        "${ICON_DIR}/org.thepriest.george.svg" 2>/dev/null || true
  ok "installed to ${APP_DIR}"
}

make_launcher() {
  cat > "${BIN_DIR}/george" <<LAUNCH
#!/usr/bin/env bash
# George launcher - generated by install.sh
exec python3 "${APP_DIR}/george.py" "\$@"
LAUNCH
  chmod 0755 "${BIN_DIR}/george"
  ok "launcher: ${BIN_DIR}/george"
  case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) warn "${BIN_DIR} is not on your PATH"
       dim "add this to ~/.bashrc or ~/.zshrc:"
       dim "  export PATH=\"\${PATH}:${BIN_DIR}\"" ;;
  esac
}

make_desktop() {
  cat > "${DESKTOP_DIR}/com.thepriest.george.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=George
GenericName=Local AI Assistant
Comment=Fully local desktop AI - Ollama, no API keys, no cloud
Exec=${BIN_DIR}/george
Icon=com.thepriest.george
Terminal=false
Categories=Utility;Development;
Keywords=ai;assistant;ollama;local;jarvis;
StartupNotify=true
StartupWMClass=com.thepriest.george
DESK
  have update-desktop-database && \
    update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
  have gtk-update-icon-cache && \
    gtk-update-icon-cache -qtf "${PREFIX}/share/icons/hicolor" >/dev/null 2>&1 || true
  ok "desktop entry installed"
}

check_gtk() {
  if python3 - <<'PYCHK' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
PYCHK
  then
    ok "GTK4 + libadwaita bindings work"
    if python3 -c "import cairo" >/dev/null 2>&1; then
      ok "pycairo present - the animated HUD is live"
    else
      warn "pycairo is missing - the HUD falls back to plain widgets"
      dim "  it is in the core package list above; re-run --deps-only"
    fi
  else
    warn "python could not import GTK4/libadwaita"
    dim "George will not start until that is fixed. Try:"
    dim "  $(core_packages)"
    return 1
  fi
}

# ------------------------------------------------------- uninstall ----
uninstall() {
  banner
  say "removing George"
  rm -rf "${APP_DIR}"
  rm -f "${BIN_DIR}/george"
  rm -f "${DESKTOP_DIR}/com.thepriest.george.desktop"
  rm -f "${ICON_DIR}/com.thepriest.george.svg"
  rm -f "${DESKTOP_DIR}/org.thepriest.george.desktop"
  rm -f "${ICON_DIR}/org.thepriest.george.svg"
  ok "app removed"
  if ask_destructive "also delete your config, memory, notes and saved chats?"; then
    rm -rf "${CONFIG_DIR}" "${DATA_DIR}"
    ok "personal data removed"
  else
    dim "kept ${CONFIG_DIR} and ${DATA_DIR}"
  fi
  dim "ollama and the models were left alone - remove them yourself if you want"
  exit 0
}

# ------------------------------------------------------------ main ----
while [ "$#" -gt 0 ]; do
  case "$1" in
    --uninstall) DO_UNINSTALL=1 ;;
    --yes|-y)    ASSUME_YES=1 ;;
    --no-model)  DO_MODEL=0 ;;
    --deps-only) DEPS_ONLY=1 ;;
    --no-deps)   DO_DEPS=0 ;;
    --allow-remote-ollama) ALLOW_REMOTE_OLLAMA=1 ;;
    --model)     shift; MODEL="${1:-$MODEL}" ;;
    --model=*)   MODEL="${1#--model=}" ;;
    --help|-h)
      sed -n '2,16p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) warn "ignoring unknown flag: $1" ;;
  esac
  shift
done

[ "${DO_UNINSTALL}" = "1" ] && uninstall

banner
detect_distro
mkdir -p "${DATA_DIR}"

if [ -z "${PKG}" ]; then
  warn "no package manager I recognise - I will still install the app files"
  dim "you need: python3, PyGObject, GTK4, libadwaita, and ollama"
else
  ok "${DISTRO_NAME:-linux}  (${PKG})"
  case "${DISTRO_ID}" in
    cachyos) dim "CachyOS detected - using the Arch path with the tuned repos" ;;
  esac
fi

if [ -n "${PKG}" ] && [ "${DO_DEPS}" = "1" ]; then
  say "installing the GTK4 stack"
  # shellcheck disable=SC2046
  pkg_install $(core_packages) || die "dependency install failed"
  ok "core dependencies in place"
  say "installing optional extras (voice, media, clipboard, screenshots)"
  # shellcheck disable=SC2046
  pkg_install_soft $(extra_packages)
fi

check_gtk || true
ensure_ollama || true

if [ "${DEPS_ONLY}" = "1" ]; then
  ok "dependencies only - stopping here"
  exit 0
fi

resolve_source
verify_source
install_files
make_launcher
make_desktop
pull_model

[ -n "${CLEANUP_SRC}" ] && rm -rf "${CLEANUP_SRC}"

echo
ok "George is installed"
dim "run it:        george"
dim "or find it in your application menu"
dim "config:        ${CONFIG_DIR}/config.json"
dim "uninstall:     bash ${APP_DIR}/install.sh --uninstall   (or re-run this script with --uninstall)"
echo
say "ollama starts with George and stops when you close it"
