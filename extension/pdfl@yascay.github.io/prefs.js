import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const PANEL_BOXES = ['left', 'center', 'right'];

export default class PdflPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        const page = new Adw.PreferencesPage({
            title: 'Power Display',
            icon_name: 'video-display-symbolic',
        });

        const panelGroup = new Adw.PreferencesGroup({
            title: 'Panel Icon &amp; Shortcut',
            description: 'Position of the indicator icon in the GNOME top bar.',
        });

        const boxModel = Gtk.StringList.new(['Left', 'Center', 'Right']);
        const boxRow = new Adw.ComboRow({
            title: 'Panel Box',
            model: boxModel,
        });
        const currentBox = settings.get_string('panel-box');
        const idx = PANEL_BOXES.indexOf(currentBox);
        boxRow.selected = idx >= 0 ? idx : 2;
        boxRow.connect('notify::selected', () => {
            const chosen = PANEL_BOXES[boxRow.selected] || 'right';
            settings.set_string('panel-box', chosen);
        });
        panelGroup.add(boxRow);

        const posRow = new Adw.SpinRow({
            title: 'Position in Box',
            adjustment: new Gtk.Adjustment({
                lower: 0,
                upper: 20,
                step_increment: 1,
                value: settings.get_int('panel-position'),
            }),
        });
        posRow.connect('notify::value', () => {
            settings.set_int('panel-position', Math.round(posRow.value));
        });
        panelGroup.add(posRow);

        const shortcuts = settings.get_strv('toggle-flyout');
        const shortcutRow = new Adw.ActionRow({
            title: 'Flyout Shortcut',
            subtitle: shortcuts.join(', ') || '<Super><Alt>d',
        });
        panelGroup.add(shortcutRow);
        page.add(panelGroup);

        const appGroup = new Adw.PreferencesGroup({
            title: 'Full Settings &amp; Profiles',
        });
        const launchRow = new Adw.ActionRow({
            title: 'Open Power Display Settings',
            subtitle: 'Manage profiles, VCP diagnostics, mouse wheel, and theme integration',
        });
        const openBtn = new Gtk.Button({
            label: 'Open',
            valign: Gtk.Align.CENTER,
            css_classes: ['suggested-action'],
        });
        openBtn.connect('clicked', () => {
            try {
                Gio.Subprocess.new(['pdfl-settings'], Gio.SubprocessFlags.NONE);
            } catch (_e) {
                // Fallback if not in PATH
            }
        });
        launchRow.add_suffix(openBtn);
        launchRow.activatable_widget = openBtn;
        appGroup.add(launchRow);
        page.add(appGroup);

        window.add(page);
    }
}
