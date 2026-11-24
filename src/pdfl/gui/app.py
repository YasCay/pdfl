"""Adwaita Application entry point for pdfl-settings."""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from pdfl import APP_ID
from pdfl.gui.window import PdflMainWindow


class PdflApplication(Adw.Application):
    """GTK4/libadwaita Settings & Profiles application."""

    def __init__(
        self,
        demo: bool = False,
        direct: bool = False,
        initial_page: str = "displays",
        smoke_test: bool = False,
    ) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.demo = demo
        self.direct = direct
        self.initial_page = initial_page
        self.smoke_test = smoke_test
        self._window: PdflMainWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = PdflMainWindow(
                self,
                demo=self.demo,
                direct=self.direct,
                initial_page=self.initial_page,
            )
        self._window.present()
        if self.smoke_test:
            GLib.timeout_add(150, self._run_smoke_step_profiles)

    def _run_smoke_step_profiles(self) -> bool:
        if self._window:
            self._window.switch_page("profiles")
        GLib.timeout_add(100, self._run_smoke_step_settings)
        return False

    def _run_smoke_step_settings(self) -> bool:
        if self._window:
            self._window.switch_page("settings")
        GLib.timeout_add(100, self._finish_smoke_test)
        return False

    def _finish_smoke_test(self) -> bool:
        if self._window:
            self._window.close()
        self.quit()
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdfl-settings",
        description="Power Display for Linux — GTK4/libadwaita settings window",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use demo backend without hardware access",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Use local backend directly instead of D-Bus",
    )
    parser.add_argument(
        "--page",
        choices=["displays", "profiles", "settings"],
        default="displays",
        help="Initial page in the settings window",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Briefly open the window, switch tabs, and exit cleanly",
    )
    args, remaining = parser.parse_known_args(argv)
    app = PdflApplication(
        demo=args.demo,
        direct=args.direct,
        initial_page=args.page,
        smoke_test=args.smoke_test,
    )
    return app.run([sys.argv[0], *remaining])


if __name__ == "__main__":
    raise SystemExit(main())
