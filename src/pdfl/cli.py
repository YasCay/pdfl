"""Command-line interface for Power Display for Linux (pdfl)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from pdfl import __version__
from pdfl.backend import PowerDisplayBackend
from pdfl.models import Profile, ProfileMonitorSetting

FEATURE_ALIASES: dict[str, str] = {
    "brightness": "brightness",
    "contrast": "contrast",
    "volume": "volume",
    "color-temp": "color_temperature",
    "color_temperature": "color_temperature",
    "input": "input_source",
    "input_source": "input_source",
    "power": "power_state",
    "power_state": "power_state",
}


def _parse_numeric_value(raw: str) -> int:
    return int(raw, 0)


def _get_executor(demo: bool, direct: bool) -> tuple[str, Any]:
    if not direct:
        try:
            from pdfl.dbus_client import PdflDbusClient

            client = PdflDbusClient()
            if client.is_available():
                return "dbus", client
        except Exception:
            pass
    backend = PowerDisplayBackend(demo=demo)
    backend.initialize()
    return "direct", backend


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_monitors(state: dict[str, Any]) -> None:
    if state.get("demo"):
        print("[Demo mode active — no real monitor changes]")
    for warn in state.get("warnings", []):
        print(f"Warning: {warn}")
    monitors = state.get("monitors", [])
    if not monitors:
        print("No supported monitors detected.")
        return
    for mon in monitors:
        caps = mon.get("capabilities", {})
        tag = "Built-in" if mon.get("is_builtin") else "External"
        prim = " [Primary]" if mon.get("is_primary") else ""
        hid = " [Hidden]" if mon.get("hidden") else ""
        print(
            f"#{mon.get('monitor_number', '?')} {mon['display_name']} "
            f"({tag}, {mon.get('connector') or '-'}){prim}{hid}"
        )
        print(f"   ID: {mon['monitor_id']}")
        if not mon.get("is_builtin"):
            ddc_status = mon.get("ddc_status", "not_applicable")
            bus_num = mon.get("i2c_bus")
            bus_str = f"/dev/i2c-{bus_num}" if bus_num is not None else "—"
            drm_label = mon.get("drm_connector") or mon.get("connector") or "-"
            ddc_labels = {
                "available": f"available ({bus_str})",
                "bus_locked": f"bus locked ({bus_str})",
                "ddc_unresponsive": f"unresponsive ({bus_str})",
                "permission_denied": f"permission denied ({bus_str})",
                "no_i2c_bus": "no I2C bus",
                "timeout": f"timeout ({bus_str})",
                "error": f"error ({bus_str})",
                "not_applicable": "not applicable",
            }
            ddc_desc = ddc_labels.get(ddc_status, ddc_status)
            print(f"   Status: DRM {drm_label} connected · DDC/CI: {ddc_desc}")
        parts: list[str] = []
        if caps.get("brightness"):
            if str(mon.get("brightness_mode", "")).startswith("software_"):
                suffix = " (software dimming)"
            elif not caps.get("brightness_writable"):
                suffix = " (read-only)"
            else:
                suffix = ""
            parts.append(f"Brightness={mon.get('brightness')}%{suffix}")
        if caps.get("contrast"):
            parts.append(f"Contrast={mon.get('contrast')}%")
        if caps.get("volume"):
            parts.append(f"Volume={mon.get('volume')}%")
        if caps.get("color_temperature"):
            parts.append(f"ColorTemp=0x{(mon.get('color_temperature') or 0):02X}")
        if caps.get("input_source"):
            parts.append(f"Input=0x{(mon.get('input_source') or 0):02X}")
        if caps.get("power_state"):
            parts.append(f"Power=0x{(mon.get('power_state') or 0):02X}")
        if caps.get("rotation"):
            parts.append(f"Rotation={mon.get('rotation', 0)}°")
        print("   Values: " + (", ".join(parts) if parts else "none"))
        if mon.get("status_level") == "info" and mon.get("status_notice"):
            print(f"   Info: {mon['status_notice']}")
        elif mon.get("last_error"):
            print(f"   Error: {mon['last_error']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfl",
        description="Power Display for Linux (pdfl) — monitor and profile control",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use demo backend without hardware access (when running with --direct)",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Bypass D-Bus and use the local backend directly",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current monitor and settings status")
    sub.add_parser("list", help="List detected monitors")
    sub.add_parser("rescan", help="Rescan connected monitors")

    p_set = sub.add_parser("set", help="Set a monitor feature")
    p_set.add_argument("monitor_id")
    p_set.add_argument("feature", choices=sorted(FEATURE_ALIASES.keys()))
    p_set.add_argument("value", help="Percentage (0-100) or VCP value (e.g. 0x11)")
    p_set.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm potentially disruptive actions such as input switch or standby/off",
    )

    p_linked = sub.add_parser("linked", help="Control linked brightness")
    p_linked_sub = p_linked.add_subparsers(dest="linked_cmd", required=True)
    p_linked_sub.add_parser("on", help="Enable linked brightness")
    p_linked_sub.add_parser("off", help="Disable linked brightness")
    p_linked_set = p_linked_sub.add_parser("set", help="Set linked brightness percentage")
    p_linked_set.add_argument("percent", type=int)

    p_rot = sub.add_parser("rotate", help="Rotate monitor (with automatic safety revert)")
    p_rot.add_argument("monitor_id")
    p_rot.add_argument("degrees", type=int, choices=[0, 90, 180, 270])
    p_rot.add_argument(
        "--confirm-now",
        action="store_true",
        help="Confirm rotation permanently right away",
    )

    p_rot_conf = sub.add_parser("rotate-confirm", help="Confirm a pending rotation")
    p_rot_conf.add_argument("token")

    p_rot_rev = sub.add_parser("rotate-revert", help="Revert a pending rotation")
    p_rot_rev.add_argument("token")

    p_prof = sub.add_parser("profile", help="Manage and apply monitor profiles")
    p_prof_sub = p_prof.add_subparsers(dest="prof_cmd", required=True)
    p_prof_sub.add_parser("list", help="List all profiles")
    p_prof_apply = p_prof_sub.add_parser("apply", help="Apply profile by ID or name")
    p_prof_apply.add_argument("identifier")
    p_prof_del = p_prof_sub.add_parser("delete", help="Delete a profile")
    p_prof_del.add_argument("profile_id", type=int)
    p_prof_create = p_prof_sub.add_parser(
        "create", help="Create a new profile from current monitor values"
    )
    p_prof_create.add_argument("name")

    p_daemon = sub.add_parser("daemon", help="Run D-Bus background daemon in foreground")
    p_daemon.add_argument("--no-watchers", action="store_true")

    p_gui = sub.add_parser("settings", help="Launch GTK4/libadwaita settings window")
    p_gui.add_argument("--page", choices=["displays", "profiles", "settings"], default="displays")
    p_gui.add_argument("--smoke-test", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "daemon":
        from pdfl.daemon import main as daemon_main

        d_args = []
        if args.demo:
            d_args.append("--demo")
        if args.no_watchers:
            d_args.append("--no-watchers")
        return daemon_main(d_args)

    if args.command == "settings":
        from pdfl.gui.app import main as gui_main

        g_args = ["--page", args.page]
        if args.demo:
            g_args.append("--demo")
        if args.direct:
            g_args.append("--direct")
        if args.smoke_test:
            g_args.append("--smoke-test")
        return gui_main(g_args)

    mode, target = _get_executor(demo=args.demo, direct=args.direct)
    try:
        if args.command in ("status", "list"):
            state = target.get_state()
            if args.json:
                _print_json(state)
            else:
                _print_monitors(state)
            return 0

        if args.command == "rescan":
            state = target.rescan()
            if args.json:
                _print_json(state)
            else:
                _print_monitors(state)
            return 0

        if args.command == "set":
            feat = FEATURE_ALIASES[args.feature]
            val = _parse_numeric_value(args.value)
            if mode == "dbus":
                res = target.set_feature(args.monitor_id, feat, val, confirmed=args.confirm)
            else:
                res = target.set_feature(args.monitor_id, feat, val, confirmed=args.confirm).to_dict()
            if args.json:
                _print_json(res)
            else:
                print(res.get("message", ""))
            return 0 if res.get("ok") else 2

        if args.command == "linked":
            if args.linked_cmd in ("on", "off"):
                state = target.set_linked_mode(args.linked_cmd == "on")
                if args.json:
                    _print_json(state)
                else:
                    print(
                        f"Linked brightness: {'on' if args.linked_cmd == 'on' else 'off'} "
                        f"(Master={state.get('linked_brightness_value')})"
                    )
                return 0
            if mode == "dbus":
                res = target.set_linked_brightness(args.percent)
            else:
                res = target.set_linked_brightness(args.percent).to_dict()
            if args.json:
                _print_json(res)
            else:
                print(res.get("message", ""))
            return 0 if res.get("ok") else 2

        if args.command == "rotate":
            if mode == "dbus":
                res = target.request_rotation(args.monitor_id, args.degrees)
            else:
                res = target.request_rotation(args.monitor_id, args.degrees).to_dict()
            if res.get("ok") and args.confirm_now:
                token = res["data"]["token"]
                if mode == "dbus":
                    res = target.confirm_rotation(token)
                else:
                    res = target.confirm_rotation(token).to_dict()
            if args.json:
                _print_json(res)
            else:
                print(res.get("message", ""))
                if res.get("data", {}).get("token") and not args.confirm_now:
                    print(f"Confirmation token: {res['data']['token']}")
            return 0 if res.get("ok") else 2

        if args.command == "rotate-confirm":
            res = (
                target.confirm_rotation(args.token)
                if mode == "dbus"
                else target.confirm_rotation(args.token).to_dict()
            )
            if args.json:
                _print_json(res)
            else:
                print(res.get("message", ""))
            return 0 if res.get("ok") else 2

        if args.command == "rotate-revert":
            res = (
                target.revert_rotation(args.token)
                if mode == "dbus"
                else target.revert_rotation(args.token).to_dict()
            )
            if args.json:
                _print_json(res)
            else:
                print(res.get("message", ""))
            return 0 if res.get("ok") else 2

        if args.command == "profile":
            if args.prof_cmd == "list":
                profs = (
                    target.get_profiles()
                    if mode == "dbus"
                    else [p.to_dict() for p in target.get_profiles()]
                )
                if args.json:
                    _print_json(profs)
                else:
                    if not profs:
                        print("No saved profiles.")
                    for p in profs:
                        print(f"[{p['id']}] {p['name']} ({len(p['monitor_settings'])} monitors)")
                return 0

            if args.prof_cmd == "apply":
                res = (
                    target.apply_profile(args.identifier)
                    if mode == "dbus"
                    else target.apply_profile(args.identifier).to_dict()
                )
                if args.json:
                    _print_json(res)
                else:
                    print(res.get("message", ""))
                return 0 if res.get("ok") else 2

            if args.prof_cmd == "delete":
                deleted = target.delete_profile(args.profile_id)
                if args.json:
                    _print_json({"deleted": deleted, "id": args.profile_id})
                else:
                    print("Profile deleted." if deleted else "Profile not found.")
                return 0 if deleted else 2

            if args.prof_cmd == "create":
                state = target.get_state()
                entries = []
                for mon in state.get("monitors", []):
                    caps = mon.get("capabilities", {})
                    entries.append(
                        ProfileMonitorSetting(
                            monitor_id=mon["monitor_id"],
                            brightness=mon.get("brightness") if caps.get("brightness") else None,
                            contrast=mon.get("contrast") if caps.get("contrast") else None,
                            volume=mon.get("volume") if caps.get("volume") else None,
                            color_temperature=(
                                mon.get("color_temperature")
                                if caps.get("color_temperature")
                                else None
                            ),
                        )
                    )
                prof = Profile(id=0, name=args.name, monitor_settings=entries)
                saved = (
                    target.save_profile(prof.to_dict())
                    if mode == "dbus"
                    else target.save_profile(prof).to_dict()
                )
                if args.json:
                    _print_json(saved)
                else:
                    print(f"Profile [{saved['id']}] '{saved['name']}' saved.")
                return 0
    finally:
        if mode == "direct":
            target.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
