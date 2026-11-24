#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

AUTOSTART=0
DEMO_DAEMON=0
DRY_RUN=0
BINDIR="${XDG_BIN_HOME:-${HOME}/.local/bin}"
DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --autostart)
      AUTOSTART=1
      shift
      ;;
    --demo-daemon)
      DEMO_DAEMON=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --bindir)
      BINDIR="$2"
      shift 2
      ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--autostart] [--demo-daemon] [--dry-run] [--bindir DIR]

Installs Power Display for Linux (pdfl) reversibly into the user's home directory only.
No sudo is required and no system files (such as /etc/udev) are modified.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

PKG_HOME="${DATA_HOME}/pdfl"
EXT_DIR="${DATA_HOME}/gnome-shell/extensions/pdfl@yascay.github.io"
APP_DIR="${DATA_HOME}/applications"
ICON_APPS_DIR="${DATA_HOME}/icons/hicolor/scalable/apps"
ICON_STATUS_DIR="${DATA_HOME}/icons/hicolor/scalable/status"
DBUS_DIR="${DATA_HOME}/dbus-1/services"
SYSTEMD_DIR="${CONFIG_HOME}/systemd/user"
AUTOSTART_DIR="${CONFIG_HOME}/autostart"

DAEMON_EXEC="${BINDIR}/pdfl-daemon"
if [[ "${DEMO_DAEMON}" -eq 1 ]]; then
  DAEMON_EXEC="${BINDIR}/pdfl-daemon --demo"
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[Dry-Run] Would install to:"
  echo "  Code:      ${PKG_HOME}/src"
  echo "  Binaries:  ${BINDIR}/{pdfl,pdfl-daemon,pdfl-settings}"
  echo "  Extension: ${EXT_DIR}"
  echo "  Desktop:   ${APP_DIR}/io.github.yascay.pdfl.desktop"
  echo "  Icons:     ${ICON_APPS_DIR} & ${ICON_STATUS_DIR}"
  echo "  D-Bus:     ${DBUS_DIR}/io.github.yascay.pdfl.Daemon.service"
  echo "  Systemd:   ${SYSTEMD_DIR}/pdfl-daemon.service"
  if [[ "${AUTOSTART}" -eq 1 ]]; then
    echo "  Autostart: ${AUTOSTART_DIR}/io.github.yascay.pdfl-daemon.desktop"
  fi
  exit 0
fi

mkdir -p "${BINDIR}" "${PKG_HOME}" "${EXT_DIR}" "${APP_DIR}" \
  "${ICON_APPS_DIR}" "${ICON_STATUS_DIR}" "${DBUS_DIR}" "${SYSTEMD_DIR}"

# 1. Copy Python package
rm -rf "${PKG_HOME}/src"
cp -a "${REPO_ROOT}/src" "${PKG_HOME}/src"

# 2. Install wrapper launchers pointing to installed PKG_HOME/src
PKG_SRC_JSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "${PKG_HOME}/src")"
for cmd_name in pdfl pdfl-daemon pdfl-settings; do
  case "${cmd_name}" in
    pdfl) entry="pdfl.cli" ;;
    pdfl-daemon) entry="pdfl.daemon" ;;
    pdfl-settings) entry="pdfl.gui.app" ;;
  esac
  cat > "${BINDIR}/${cmd_name}" <<EOF
#!/usr/bin/env python3
import sys
_SRC = ${PKG_SRC_JSON}
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from ${entry} import main
if __name__ == "__main__":
    raise SystemExit(main())
EOF
  chmod +x "${BINDIR}/${cmd_name}"
done

# 3. Install GNOME Shell 50 extension and compile GSettings schemas
rm -rf "${EXT_DIR}"
mkdir -p "${EXT_DIR}"
cp -a "${REPO_ROOT}/extension/pdfl@yascay.github.io/." "${EXT_DIR}/"
if command -v glib-compile-schemas >/dev/null 2>&1; then
  glib-compile-schemas --strict "${EXT_DIR}/schemas"
fi

# 4. Install icons & .desktop launcher
cp -f "${REPO_ROOT}/data/icons/hicolor/scalable/apps/io.github.yascay.pdfl.svg" \
  "${ICON_APPS_DIR}/io.github.yascay.pdfl.svg"
cp -f "${REPO_ROOT}/data/icons/hicolor/scalable/status/io.github.yascay.pdfl-symbolic.svg" \
  "${ICON_STATUS_DIR}/io.github.yascay.pdfl-symbolic.svg"
ESC_BINDIR="${BINDIR//\\/\\\\}"
ESC_BINDIR="${ESC_BINDIR//&/\\&}"
ESC_BINDIR="${ESC_BINDIR//|/\\|}"
sed "s|^Exec=pdfl-settings|Exec=${ESC_BINDIR}/pdfl-settings|" \
  "${REPO_ROOT}/data/applications/io.github.yascay.pdfl.desktop" \
  > "${APP_DIR}/io.github.yascay.pdfl.desktop"

if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
  gtk4-update-icon-cache -f -t "${DATA_HOME}/icons/hicolor" >/dev/null 2>&1 || true
fi

# 5. Install D-Bus session activation & systemd user unit
ESC_DAEMON_EXEC="${DAEMON_EXEC//\\/\\\\}"
ESC_DAEMON_EXEC="${ESC_DAEMON_EXEC//&/\\&}"
ESC_DAEMON_EXEC="${ESC_DAEMON_EXEC//|/\\|}"
sed "s|@DAEMON_EXEC@|${ESC_DAEMON_EXEC}|g" \
  "${REPO_ROOT}/data/dbus-1/io.github.yascay.pdfl.Daemon.service.in" \
  > "${DBUS_DIR}/io.github.yascay.pdfl.Daemon.service"
sed "s|@DAEMON_EXEC@|${ESC_DAEMON_EXEC}|g" \
  "${REPO_ROOT}/data/systemd/pdfl-daemon.service.in" \
  > "${SYSTEMD_DIR}/pdfl-daemon.service"

# 6. Optional XDG Autostart
if [[ "${AUTOSTART}" -eq 1 ]]; then
  mkdir -p "${AUTOSTART_DIR}"
  sed "s|@DAEMON_EXEC@|${ESC_DAEMON_EXEC}|g" \
    "${REPO_ROOT}/data/autostart/io.github.yascay.pdfl-daemon.desktop.in" \
    > "${AUTOSTART_DIR}/io.github.yascay.pdfl-daemon.desktop"
fi

cat <<EOF
Installation completed successfully (user directory only).

Next steps to enable on Ubuntu 26.04 (GNOME Shell 50.1 / Wayland):
  1. Ensure ${BINDIR} is in your PATH.
  2. Start the background daemon (or via systemd --user):
       systemctl --user daemon-reload
       systemctl --user enable --now pdfl-daemon.service
     (Or run safely in demo mode without hardware writes: ${BINDIR}/pdfl-daemon --demo)
  3. Enable the GNOME Shell extension:
       gnome-extensions enable pdfl@yascay.github.io
     Note: Under Wayland, GNOME Shell picks up newly copied extensions after logging out and back in.
  4. Open Settings:
       ${BINDIR}/pdfl-settings   (or launch "Power Display for Linux" from the app grid)
EOF
