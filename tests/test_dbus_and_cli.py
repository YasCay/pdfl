"""Integration tests for pdfl-daemon + D-Bus RPC + CLI in an isolated dbus-run-session."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class DbusAndCliTests(unittest.TestCase):
    def test_cli_direct_demo_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XDG_CONFIG_HOME"] = f"{tmp}/config"
            env["XDG_STATE_HOME"] = f"{tmp}/state"
            env["PYTHONPATH"] = str(REPO_ROOT / "src")

            res_list = subprocess.run(
                [sys.executable, "-m", "pdfl.cli", "--demo", "--direct", "--json", "list"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(res_list.returncode, 0, res_list.stderr)
            payload = json.loads(res_list.stdout)
            self.assertTrue(payload["demo"])
            self.assertEqual(len(payload["monitors"]), 3)

            # Confirm input requires --confirm
            res_unconf = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pdfl.cli",
                    "--demo",
                    "--direct",
                    "--json",
                    "set",
                    "demo:ddc:DEL:U2723QE:CN04K921",
                    "input",
                    "0x11",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(res_unconf.returncode, 2)
            self.assertTrue(json.loads(res_unconf.stdout)["requires_confirmation"])

            res_conf = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pdfl.cli",
                    "--demo",
                    "--direct",
                    "--json",
                    "set",
                    "demo:ddc:DEL:U2723QE:CN04K921",
                    "input",
                    "0x11",
                    "--confirm",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(res_conf.returncode, 0, res_conf.stderr)

    def test_daemon_and_cli_over_private_dbus_session(self) -> None:
        script = """
import json, os, subprocess, sys, time
sys.path.insert(0, os.environ["PDFL_SRC"])
from pdfl.dbus_client import PdflDbusClient

proc = subprocess.Popen(
    [sys.executable, "-m", "pdfl.daemon", "--demo", "--no-watchers"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
try:
    client = PdflDbusClient(timeout_ms=5000)
    ready = False
    for _ in range(50):
        if client.is_available():
            st = client.get_state()
            if len(st.get("monitors", [])) == 3:
                ready = True
                break
        time.sleep(0.1)
    assert ready, "Daemon did not become ready on private D-Bus"

    # Set brightness via D-Bus
    r = client.set_feature("demo:ddc:DEL:U2723QE:CN04K921", "brightness", 82, True, False)
    assert r["ok"], r

    # Linked mode + step via wheel
    st_link = client.set_linked_mode(True)
    assert st_link["settings"]["linked_brightness"] is True
    step_res = client.step_brightness(5, "all")
    assert step_res["ok"], step_res

    # Save & apply profile via D-Bus
    prof = client.save_profile({
        "id": 0,
        "name": "DBusTest",
        "monitor_settings": [{"monitor_id": "demo:ddc:DEL:U2723QE:CN04K921", "brightness": 55}],
    })
    assert prof["id"] == 1
    app_res = client.apply_profile("1")
    assert app_res["ok"], app_res

    # Rotation request + revert over D-Bus
    rot = client.request_rotation("demo:internal:eDP-1", 90)
    assert rot["ok"], rot
    rev = client.revert_rotation(rot["data"]["token"])
    assert rev["ok"], rev

    # Verify CLI talks to the same running daemon
    cli_out = subprocess.check_output(
        [sys.executable, "-m", "pdfl.cli", "--json", "status"],
        text=True,
    )
    cli_state = json.loads(cli_out)
    dell = next(m for m in cli_state["monitors"] if "DEL" in m["monitor_id"])
    assert dell["brightness"] == 55, dell
finally:
    proc.terminate()
    proc.wait(timeout=5)
"""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["XDG_CONFIG_HOME"] = f"{tmp}/config"
            env["XDG_STATE_HOME"] = f"{tmp}/state"
            env["PDFL_SRC"] = str(REPO_ROOT / "src")
            env["PYTHONPATH"] = str(REPO_ROOT / "src")

            bus_cfg = pathlib.Path(tmp) / "dbus-session.conf"
            bus_cfg.write_text(
                """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <apparmor mode="disabled"/>
  <listen>unix:tmpdir=/tmp</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
    <allow user="*"/>
  </policy>
</busconfig>
""",
                encoding="utf-8",
            )

            proc = subprocess.run(
                ["dbus-run-session", f"--config-file={bus_cfg}", "--", sys.executable, "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
