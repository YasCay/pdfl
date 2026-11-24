"""Validation tests for GNOME 50 extension, GSettings, desktop files, icons, and user installer."""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT_DIR = REPO_ROOT / "extension" / "pdfl@yascay.github.io"


class ArtifactsAndInstallTests(unittest.TestCase):
    def test_extension_metadata_and_schema(self) -> None:
        meta = json.loads((EXT_DIR / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["uuid"], "pdfl@yascay.github.io")
        self.assertIn("50", meta["shell-version"])
        self.assertEqual(meta["settings-schema"], "org.gnome.shell.extensions.pdfl")

        res = subprocess.run(
            ["glib-compile-schemas", "--strict", "--dry-run", str(EXT_DIR / "schemas")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_extension_js_syntax_and_gnome50_static_checks(self) -> None:
        js_files = (
            "extension.js",
            "dbusClient.js",
            "prefs.js",
            "quickSettingsModel.js",
            "quickSettingsBrightness.js",
            "tooltip.js",
        )
        for js_file in js_files:
            path = EXT_DIR / js_file
            chk = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(chk.returncode, 0, f"{js_file}: {chk.stderr}")
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("add_actor", src, f"{js_file} must not use deprecated add_actor")
            self.assertNotIn("imports.ui", src, f"{js_file} must use GNOME 50 ESM imports")
            self.assertNotIn("call_sync", src, f"{js_file} must not block shell thread with sync D-Bus")
            self.assertNotIn("new_for_bus_sync", src, f"{js_file} must not use sync D-Bus proxy init")
            self.assertNotIn("vertical: true", src, f"{js_file} must use Clutter.Orientation.VERTICAL")

    def test_quick_settings_model_and_integration(self) -> None:
        model_url = (EXT_DIR / "quickSettingsModel.js").as_uri()
        script = f"""
import assert from 'node:assert/strict';
import {{buildQuickBrightnessView, selectQuickSettingsMonitor, formatDdcShortStatus}} from '{model_url}';

const builtinOnly = {{
    monitors: [{{
        monitor_id: 'internal:eDP-1:LEN:0x9052',
        display_name: 'Built-in Display',
        is_builtin: true,
        hidden: false,
        capabilities: {{brightness: true, brightness_writable: false}},
        brightness: 55,
    }}],
}};

const benqUnresponsive = {{
    monitors: [
        ...builtinOnly.monitors,
        {{
            monitor_id: 'ddc:BNQ:BenQ_EW3270U:DEMO0001234',
            display_name: 'BenQ EW3270U',
            manufacturer: 'BNQ',
            model: 'BenQ EW3270U',
            connector: 'DP-1',
            is_builtin: false,
            is_primary: true,
            hidden: false,
            ddc_status: 'ddc_unresponsive',
            i2c_bus: 9,
            brightness: null,
            last_error: 'DRM monitor detected, but DDC/CI on /dev/i2c-9 did not respond.',
            capabilities: {{brightness: false, brightness_writable: false}},
        }},
    ],
}};

const vUnresp = buildQuickBrightnessView({{isConnected: true, state: benqUnresponsive}});
assert.equal(vUnresp.mode, 'status');
assert.equal(vUnresp.visible, true);
assert.equal(vUnresp.title, 'BenQ EW3270U');
assert.match(vUnresp.shortStatus, /No DDC\\/CI response/);
assert.match(vUnresp.shortStatus, /DP-1/);
assert.equal(vUnresp.menuSubtitle, 'DP-1 · /dev/i2c-9');

const benqWritable = {{
    monitors: [
        ...builtinOnly.monitors,
        {{
            monitor_id: 'ddc:DEL:Dell_U2723QE:ABC',
            display_name: 'Dell U2723QE',
            manufacturer: 'DEL',
            model: 'Dell U2723QE',
            connector: 'HDMI-1',
            is_builtin: false,
            is_primary: true,
            hidden: false,
            ddc_status: 'available',
            i2c_bus: 6,
            brightness: 40,
            capabilities: {{brightness: true, brightness_writable: true}},
        }},
        {{
            monitor_id: 'ddc:BNQ:BenQ_EW3270U:DEMO0001234',
            display_name: 'BenQ EW3270U',
            manufacturer: 'BNQ',
            model: 'BenQ EW3270U',
            connector: 'DP-1',
            is_builtin: false,
            is_primary: false,
            hidden: false,
            ddc_status: 'available',
            i2c_bus: 9,
            brightness: 68,
            capabilities: {{brightness: true, brightness_writable: true}},
        }},
    ],
}};

const vSlider = buildQuickBrightnessView({{isConnected: true, state: benqWritable}});
assert.equal(vSlider.mode, 'slider');
assert.equal(vSlider.monitorId, 'ddc:BNQ:BenQ_EW3270U:DEMO0001234');
assert.equal(vSlider.brightness, 68);
assert.equal(vSlider.externalMonitors.length, 2);

assert.match(formatDdcShortStatus({{ddc_status: 'bus_locked', connector: 'DP-1'}}), /DDC bus locked/);
assert.match(formatDdcShortStatus({{ddc_status: 'timeout', connector: 'DP-1'}}), /timed out/);
assert.match(formatDdcShortStatus({{ddc_status: 'permission_denied', connector: 'DP-1'}}), /No I2C access/);
assert.match(formatDdcShortStatus({{ddc_status: 'no_i2c_bus', connector: 'DP-1'}}), /No DDC\\/CI bus/);

const vOffline = buildQuickBrightnessView({{isConnected: false, state: null}});
assert.equal(vOffline.mode, 'status');
assert.equal(vOffline.visible, true);

const vBuiltin = buildQuickBrightnessView({{isConnected: true, state: builtinOnly}});
assert.equal(vBuiltin.mode, 'hidden');
assert.equal(vBuiltin.visible, false);
assert.equal(selectQuickSettingsMonitor(builtinOnly), null);

const benqSoftwareFallback = {{
    settings: {{software_dimming_fallback: true}},
    monitors: [
        ...builtinOnly.monitors,
        {{
            monitor_id: 'ddc:BNQ:BenQ_EW3270U:DEMO0001234',
            display_name: 'BenQ EW3270U',
            manufacturer: 'BNQ',
            model: 'BenQ EW3270U',
            connector: 'DP-1',
            is_builtin: false,
            is_primary: true,
            hidden: false,
            ddc_status: 'ddc_unresponsive',
            i2c_bus: 9,
            brightness: 70,
            contrast: 55,
            color_temperature: 0x04,
            available_color_presets: [
                {{value: 0x04, name: '5000K'}},
                {{value: 0x05, name: '6500K'}},
            ],
            brightness_mode: 'software_gamma',
            status_level: 'info',
            status_notice: 'Controlled in software mode (Mutter Gamma) active.',
            last_error: 'EREMOTEIO on /dev/i2c-9',
            capabilities: {{brightness: true, brightness_writable: true, contrast: true, color_temperature: true}},
        }},
    ],
}};
const vSw = buildQuickBrightnessView({{isConnected: true, state: benqSoftwareFallback}});
assert.equal(vSw.mode, 'slider');
assert.equal(vSw.isSoftwareDimming, true);
assert.equal(vSw.softwareFallbackEnabled, true);
assert.equal(vSw.canToggleSoftwareFallback, true);
assert.equal(vSw.brightness, 70);
assert.equal(vSw.statusLevel, 'info');
assert.match(vSw.infoText, /software mode/i);
assert.equal(vSw.errorText, null);
assert.equal(vSw.hasContrast, true);
assert.equal(vSw.contrast, 55);
assert.equal(vSw.hasColorTemperature, true);
assert.equal(vSw.colorTemperature, 0x04);
assert.equal(vSw.colorPresets.length, 2);
assert.match(vSw.title, /Software/);
assert.match(vSw.menuSubtitle, /Software mode/i);
assert.equal(vUnresp.canToggleSoftwareFallback, true);
assert.equal(vUnresp.softwareFallbackEnabled, false);
assert.equal(vUnresp.statusLevel, 'error');
assert.ok(vUnresp.errorText);
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        qs_src = (EXT_DIR / "quickSettingsBrightness.js").read_text(encoding="utf-8")
        for token in (
            "_positionBelowVolume",
            "child-added",
            "_scheduleReposition",
            "_qsGridChildAddedId",
            "_repositionIdleId",
        ):
            self.assertIn(token, qs_src, f"quickSettingsBrightness.js missing {token}")

    def test_desktop_file_and_svg_icons(self) -> None:
        desktop = REPO_ROOT / "data" / "applications" / "io.github.yascay.pdfl.desktop"
        val = subprocess.run(
            ["desktop-file-validate", str(desktop)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(val.returncode, 0, val.stdout + val.stderr)

        for svg in (
            EXT_DIR / "icons" / "pdfl-symbolic.svg",
            REPO_ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / "io.github.yascay.pdfl.svg",
            REPO_ROOT / "data" / "icons" / "hicolor" / "scalable" / "status" / "io.github.yascay.pdfl-symbolic.svg",
        ):
            ET.parse(svg)
            raw = svg.read_text(encoding="utf-8").lower()
            self.assertNotIn("microsoft", raw)
            self.assertNotIn("powertoys", raw)

    def test_reversible_user_install_and_uninstall_in_sandbox_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = pathlib.Path(tmp) / "home"
            fake_home.mkdir()
            bindir = fake_home / ".local" / "bin"
            env = dict(os.environ)
            env["HOME"] = str(fake_home)
            env["XDG_DATA_HOME"] = str(fake_home / ".local" / "share")
            env["XDG_CONFIG_HOME"] = str(fake_home / ".config")
            env["XDG_STATE_HOME"] = str(fake_home / ".local" / "state")
            env["XDG_BIN_HOME"] = str(bindir)

            inst = subprocess.run(
                [str(REPO_ROOT / "scripts" / "install-user.sh"), "--autostart"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(inst.returncode, 0, inst.stderr)

            expected = [
                bindir / "pdfl",
                bindir / "pdfl-daemon",
                bindir / "pdfl-settings",
                fake_home / ".local" / "share" / "pdfl" / "src" / "pdfl" / "backend.py",
                fake_home / ".local" / "share" / "gnome-shell" / "extensions" / "pdfl@yascay.github.io" / "extension.js",
                fake_home / ".local" / "share" / "gnome-shell" / "extensions" / "pdfl@yascay.github.io" / "quickSettingsModel.js",
                fake_home / ".local" / "share" / "gnome-shell" / "extensions" / "pdfl@yascay.github.io" / "quickSettingsBrightness.js",
                fake_home / ".local" / "share" / "gnome-shell" / "extensions" / "pdfl@yascay.github.io" / "tooltip.js",
                fake_home / ".local" / "share" / "gnome-shell" / "extensions" / "pdfl@yascay.github.io" / "schemas" / "gschemas.compiled",
                fake_home / ".local" / "share" / "applications" / "io.github.yascay.pdfl.desktop",
                fake_home / ".local" / "share" / "dbus-1" / "services" / "io.github.yascay.pdfl.Daemon.service",
                fake_home / ".config" / "systemd" / "user" / "pdfl-daemon.service",
                fake_home / ".config" / "autostart" / "io.github.yascay.pdfl-daemon.desktop",
            ]
            for path in expected:
                self.assertTrue(path.exists(), f"Missing installed artifact: {path}")

            # Verify installed pdfl binary runs in demo mode
            cli_chk = subprocess.run(
                [str(bindir / "pdfl"), "--demo", "--direct", "--json", "list"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cli_chk.returncode, 0, cli_chk.stderr)

            uninst = subprocess.run(
                [str(REPO_ROOT / "scripts" / "uninstall-user.sh"), "--purge-config"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(uninst.returncode, 0, uninst.stderr)
            for path in expected:
                self.assertFalse(path.exists(), f"Leftover artifact after uninstall: {path}")


if __name__ == "__main__":
    unittest.main()
