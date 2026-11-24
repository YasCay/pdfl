# Contributing to Power Display for Linux (`pdfl`)

Contributions are very welcome! Before opening an issue or pull request, please keep these guidelines in mind.

## Development Setup

No real external monitor is required to contribute — you can test everything safely using demo mode:

```bash
./bin/pdfl --demo --direct status
./bin/pdfl-settings --demo --direct
```

Before submitting a pull request, verify that all checks pass:

```bash
node --check extension/pdfl@yascay.github.io/*.js
glib-compile-schemas --strict --dry-run extension/pdfl@yascay.github.io/schemas
desktop-file-validate data/applications/io.github.yascay.pdfl.desktop
python3 -m unittest discover -s tests -v
```

## Privacy & Fixture Hygiene

- Never commit real monitor serial numbers, raw personal EDID dumps, host usernames, or home directory paths into tests or fixtures. Use synthetic identifiers (e.g. `DEMO0001234`, `CN04K921`).
- Keep all UI strings, CLI help text, code comments, and documentation in clear **English**.

## Pull Request Guidelines

- Keep changes focused and small, with accompanying unit tests for bug fixes or new features.
- Do not introduce `sudo`, shell interpolation (`shell=True`), synchronous D-Bus calls inside the GNOME Shell extension, or unconfirmed destructive monitor actions (`0x60` / `0xD6`).
- Respect GNOME Shell 50 ESM conventions (`Clutter.Orientation.VERTICAL`, no `add_actor` or legacy `imports.*`).
