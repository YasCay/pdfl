#!/usr/bin/env bash
set -euo pipefail

PURGE_CONFIG=0
BINDIR="${XDG_BIN_HOME:-${HOME}/.local/bin}"
DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge-config)
      PURGE_CONFIG=1
      shift
      ;;
    --bindir)
      BINDIR="$2"
      shift 2
      ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--purge-config] [--bindir DIR]

Cleanly removes all files installed by install-user.sh.
With --purge-config, saved settings and profiles are also removed.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

rm -rf "${DATA_HOME}/pdfl"
rm -rf "${DATA_HOME}/gnome-shell/extensions/pdfl@yascay.github.io"
rm -f "${BINDIR}/pdfl" "${BINDIR}/pdfl-daemon" "${BINDIR}/pdfl-settings"
rm -f "${DATA_HOME}/applications/io.github.yascay.pdfl.desktop"
rm -f "${DATA_HOME}/icons/hicolor/scalable/apps/io.github.yascay.pdfl.svg"
rm -f "${DATA_HOME}/icons/hicolor/scalable/status/io.github.yascay.pdfl-symbolic.svg"
rm -f "${DATA_HOME}/dbus-1/services/io.github.yascay.pdfl.Daemon.service"
rm -f "${CONFIG_HOME}/systemd/user/pdfl-daemon.service"
rm -f "${CONFIG_HOME}/autostart/io.github.yascay.pdfl-daemon.desktop"

if [[ "${PURGE_CONFIG}" -eq 1 ]]; then
  rm -rf "${CONFIG_HOME}/pdfl" "${STATE_HOME}/pdfl"
fi

echo "Power Display for Linux (pdfl) has been removed from the user directory."
if [[ "${PURGE_CONFIG}" -eq 0 ]]; then
  echo "Settings and profiles under ${CONFIG_HOME}/pdfl were preserved (use --purge-config to remove them too)."
fi
