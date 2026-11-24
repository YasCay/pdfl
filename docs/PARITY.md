# Parity Matrix: PowerToys Power Display vs. `pdfl` (GNOME Shell 50 / Wayland)

Status legend:
- **Available**: Fully implemented and ready for everyday desktop usage.
- **Hardware-dependent**: Fully implemented in `pdfl`; visibility and effect depend on the capabilities reported by DDC/CI, sysfs, or Mutter for the connected monitor.
- **Not implemented**: Intentionally excluded in `v1.0.0` or not practically applicable under GNOME Wayland without modifying external components.

| Feature (PowerToys Power Display) | Status in `pdfl` | Linux / GNOME / Wayland Implementation Notes |
| :--- | :--- | :--- |
| Top-bar panel icon + flyout | **Available** | GNOME Shell 50 extension (`pdfl@yascay.github.io`) with symbolic icon and native `PopupMenu` flyout. |
| GNOME Quick Settings external slider | **Available** | Native `QuickSlider` positioned directly below volume with submenu contrast &amp; color preset controls. |
| Global keyboard shortcut for flyout | **Available** | `Main.wm.addKeybinding('toggle-flyout', ...)` via schema `org.gnome.shell.extensions.pdfl` (default `<Super><Alt>d`). |
| Full keyboard navigation in flyout | **Available** | Focusable header icon buttons (`accessible_name`), arrow/Tab navigation, accessible GNOME `Slider` rows. |
| Mouse-wheel scroll on panel icon | **Hardware-dependent** | Opt-in via `mouse_wheel_mode` (`disabled` default, `primary`, `all`) and `mouse_wheel_step`; requires writable brightness. |
| External brightness (DDC/CI `0x10`) | **Hardware-dependent** | Targeted DRM/EDID bus mapping + `ddcutil 2.2.5` (`--bus`); requires `i2c-dev` and user access to `/dev/i2c-*`. Optional **opt-in** fallback (`software_dimming_fallback`, default `false`) via Mutter `SetOutputCTM`/`SetCrtcGamma` with 35% floor. |
| Internal laptop panel brightness | **Hardware-dependent** | `/sys/class/backlight/*` + `org.gnome.Mutter.DisplayConfig.SetBacklight`, `brightnessctl`, or writable sysfs; never replaced with software gamma dimming. |
| Linked Brightness | **Available** | Master slider with deterministic initial seed (`LinkedBrightnessPlanner`) and zero unsolicited hardware writes on enable. |
| Contrast (`0x12`), Volume (`0x62`) | **Hardware-dependent** | Hardware controls appear only when MCCS capabilities and `getvcp` confirm support; software contrast is available in Mutter gamma mode. |
| Color Temperature (`0x14`) | **Hardware-dependent** | Shown when reported by MCCS or software mode; hidden in flyout by default unless explicitly opted in (with warning dialog). |
| Input Source (`0x60`) &amp; Power State (`0xD6`) | **Hardware-dependent** | Requires verified MCCS support and explicit confirmation before execution. |
| Display Orientation / Rotation (`0°/90°/180°/270°`) | **Hardware-dependent** | Verified Wayland path via `org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig` (`method=0` verify, `method=1` temporary, 15s auto-revert, `method=2` persist). |
| Profiles (save, order, apply, CLI) | **Available** | Atomic XDG persistence in `profiles.json` using immutable IDs, explicit UI order, and hardware-agnostic monitor IDs. |
| Custom VCP labels (`0x14`, `0x60`, `0xD6`) | **Available** | Global and per-monitor overrides (`custom_vcp_mappings`) taking precedence over MCCS defaults. |
| VCP Capabilities diagnostics &amp; parser | **Available** | Recursive-descent parser for nested MCCS strings with full diagnostics dialog in `pdfl-settings`. |
| Per-monitor visibility &amp; hide toggles | **Available** | Configurable per monitor in the GTK4/libadwaita settings window (`monitor_prefs`). |
| Hotplug &amp; wake rescan | **Hardware-dependent** | Debounced rescan driven by `org.gnome.Mutter.DisplayConfig::MonitorsChanged` and `org.freedesktop.login1.Manager::PrepareForSleep`. |
| Theme-linked profile switching (Light Switch) | **Available** | Opt-in integration with XDG Desktop Portal (`org.freedesktop.appearance.color-scheme`), disabled by default. |
| Restore values on launch | **Available** | Opt-in (`restore_on_launch=false` by default) to prevent unexpected display changes at login. |
| Safe Demo mode (zero hardware writes) | **Available** | `pdfl --demo`, `pdfl-daemon --demo`, `pdfl-settings --demo` simulating 3 monitors including gated capabilities. |
| Monitor "Identify" screen overlay ("1, 2, 3") | **Not implemented** | GNOME Wayland intentionally restricts normal GTK windows from placing arbitrary overlays at global coordinates per monitor. |
| Windows WMI / `Dxva2.dll` / `xrandr` | **Not implemented** | Replaced by session D-Bus (`io.github.yascay.pdfl.Daemon`), `/sys/class/backlight`, `ddcutil`, and Mutter Wayland `DisplayConfig`. |
