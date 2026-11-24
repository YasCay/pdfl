# Third-Party Notices

Power Display for Linux (`pdfl`) is an independent Linux / GNOME Shell 50 / GTK4
application whose workflow and core MCCS/planner logic adapt parts of
**Microsoft PowerToys Power Display** (`src/modules/powerdisplay`), which is
licensed under the MIT License.

## Adapted Components

The following Python modules in `src/pdfl/` incorporate or adapt data tables,
algorithms, and design patterns from Microsoft PowerToys Power Display:

- `src/pdfl/vcp_names.py` — MCCS v2.2a VCP code and discrete value lookup tables
  and custom-mapping precedence (adapted from `PowerDisplay.Lib/Utils/VcpNames.cs`)
- `src/pdfl/mccs_parser.py` — Recursive-descent MCCS capabilities string parser
  (adapted from `PowerDisplay.Lib/Utils/MccsCapabilitiesParser.cs`)
- `src/pdfl/planners.py` — Linked brightness seed selection and tray/panel mouse-wheel
  adjustment planning logic (adapted from `PowerDisplay.Lib/Services/LinkedBrightnessPlanner.cs`
  and `PowerDisplay.Lib/Services/TrayWheelAdjustmentPlanner.cs`)
- `src/pdfl/models.py` / `src/pdfl/storage.py` — Profile schema ordering (`next_id`,
  explicit profile display order) and per-monitor setting model concepts

All UI assets, icons, GNOME Shell 50 extension code, GTK4/libadwaita GUI,
D-Bus daemon, Wayland/Mutter DisplayConfig integration, and Linux drivers are
original implementations for Linux/GNOME.

## Upstream License Text (Microsoft PowerToys)

```text
The MIT License

Copyright (c) Microsoft Corporation. All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
