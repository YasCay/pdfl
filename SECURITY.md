# Security Policy

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `1.0.x` | Yes |

## Reporting a Vulnerability

Please report security issues privately using **GitHub Security Advisories** ("Report a vulnerability" in the repository's **Security** tab) rather than opening a public issue.

When reporting an issue, please include:
- A clear description of the vulnerability and its potential impact.
- Steps to reproduce (prefer `--demo` mode where possible and omit personal serial numbers or local usernames).
- Your Linux distribution, GNOME Shell version, and `pdfl` version (`pdfl --version`).

## Security & Privacy Architecture

`pdfl` is designed as an **unprivileged user-space desktop utility**:

- **No Privilege Escalation:** Never invokes `sudo`, `pkexec`, or SUID helpers and never modifies `/etc/udev` or system-wide configuration. Installation stays strictly inside `$HOME/.local` and `$HOME/.config`.
- **Safe Subprocess Execution:** External tools (`ddcutil`, `brightnessctl`, `pdfl-settings`) are invoked with explicit argument arrays (`shell=False`) and strict timeouts. Session D-Bus `OpenSettings(page)` restricts arguments via a strict allowlist (`displays`, `profiles`, `settings`).
- **Private XDG Storage:** Configuration (`settings.json`, `profiles.json`) and runtime state (`monitor_state.json`) are written atomically (`mkstemp` + `fsync` + `os.replace`) inside `0700` directories with `0600` file permissions and `flock` serialization. Transient `/dev/i2c-N` bus paths are never persisted.
- **Session D-Bus Scope:** `io.github.yascay.pdfl.Daemon` binds exclusively to the user's session D-Bus (`Gio.BusType.SESSION`), not the system bus.
- **Display Safety Invariants:**
  - Disruptive hardware actions (input source switch, monitor standby/power-off) require explicit user confirmation.
  - Wayland display rotations run verification (`method=0`) first and automatically revert after 15 seconds unless explicitly confirmed.
  - Software dimming (Mutter Gamma/CTM) enforces a hard **35% minimum luminance floor**, a non-crushing S-curve contrast LUT, anti-compounding baseline extraction, and startup recovery from abnormally dark CRTC LUTs.
