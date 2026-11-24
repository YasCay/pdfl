import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Slider from 'resource:///org/gnome/shell/ui/slider.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {PdflDbusClient} from './dbusClient.js';
import {PdflQuickBrightnessIndicator} from './quickSettingsBrightness.js';
import {HoverTooltip} from './tooltip.js';

const ROTATION_CHOICES = [0, 90, 180, 270];

const PdflPanelIndicator = GObject.registerClass(
class PdflPanelIndicator extends PanelMenu.Button {
    _init(extension, client) {
        super._init(0.5, 'Power Display for Linux', false);

        this._extension = extension;
        this._client = client;
        this._state = null;
        this._pendingConfirmation = null; // { monitorId, feature, value, title }
        this._syncingSliders = false;
        this._timers = new Set();
        this._scrollAccum = 0;
        this._pendingWheelSteps = 0;
        this._wheelTimerId = 0;
        this._countdownTimerId = 0;
        this._countdownRemaining = 0;
        this._sliderTimers = new Map();
        this._tooltip = new HoverTooltip();

        const iconFile = extension.dir.get_child('icons/pdfl-symbolic.svg');
        const gicon = new Gio.FileIcon({file: iconFile});
        this._icon = new St.Icon({
            gicon,
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this.menu.actor.add_style_class_name('pdfl-menu');

        this._clientSignalIds = [
            this._client.connect('state-changed', (_c, state) => {
                this._state = state;
                this._syncCountdown(state?.pending_rotation ?? null);
                this._rebuildMenu();
            }),
            this._client.connect('connection-changed', () => {
                this._rebuildMenu();
            }),
            this._client.connect('error-occurred', (_c, _mid, message) => {
                this._lastInlineError = message;
                this._rebuildMenu();
            }),
            this._client.connect('rotation-pending', (_c, pending) => {
                this._syncCountdown(pending);
                this._rebuildMenu();
            }),
        ];

        this._menuOpenId = this.menu.connect('open-state-changed', (_menu, isOpen) => {
            if (!isOpen)
                this._tooltip?.hide();
            else if (this._client.isConnected)
                this._client.refreshStateAsync().catch(() => {});
        });
        this._tooltip.attach(
            this,
            'Open Power Display (click) · Mouse wheel if enabled');

        this._scrollId = this.connect('scroll-event', this._onPanelScroll.bind(this));
        this._rebuildMenu();
    }

    _scheduleTimeout(ms, callback) {
        let id = 0;
        id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timers.delete(id);
            return callback();
        });
        this._timers.add(id);
        return id;
    }

    _clearTimer(id) {
        if (id) {
            GLib.source_remove(id);
            this._timers.delete(id);
        }
    }

    _syncCountdown(pending) {
        this._clearTimer(this._countdownTimerId);
        this._countdownTimerId = 0;
        if (!pending) {
            this._countdownRemaining = 0;
            return;
        }
        this._countdownRemaining = Math.max(1, Math.round(pending.timeout_seconds || 15));
        this._countdownTimerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            if (this._countdownRemaining > 1) {
                this._countdownRemaining -= 1;
                if (this._countdownLabel)
                    this._countdownLabel.text = `Revert orientation in ${this._countdownRemaining}s?`;
                return GLib.SOURCE_CONTINUE;
            }
            this._countdownTimerId = 0;
            return GLib.SOURCE_REMOVE;
        });
        this._timers.add(this._countdownTimerId);
    }

    _onPanelScroll(_actor, event) {
        const settings = this._state?.settings;
        const mode = settings?.mouse_wheel_mode || 'disabled';
        if (mode === 'disabled' || !this._client.isConnected)
            return Clutter.EVENT_PROPAGATE;

        const direction = event.get_scroll_direction();
        let stepSign = 0;
        if (direction === Clutter.ScrollDirection.UP) {
            stepSign = 1;
        } else if (direction === Clutter.ScrollDirection.DOWN) {
            stepSign = -1;
        } else if (direction === Clutter.ScrollDirection.SMOOTH) {
            const [_dx, dy] = event.get_scroll_delta();
            const adjusted = (event.get_scroll_flags() & Clutter.ScrollFlags.INVERTED) ? dy : -dy;
            this._scrollAccum += adjusted;
            if (Math.abs(this._scrollAccum) >= 1.0) {
                stepSign = this._scrollAccum > 0 ? 1 : -1;
                this._scrollAccum = 0;
            }
        }

        if (stepSign === 0)
            return Clutter.EVENT_STOP;

        const stepSize = Number(settings?.mouse_wheel_step ?? 5);
        this._pendingWheelSteps += stepSign * stepSize;
        this._clearTimer(this._wheelTimerId);
        this._wheelTimerId = this._scheduleTimeout(70, () => {
            this._wheelTimerId = 0;
            const delta = this._pendingWheelSteps;
            this._pendingWheelSteps = 0;
            if (delta !== 0)
                this._client.stepBrightnessAsync(delta, '').catch(() => {});
            return GLib.SOURCE_REMOVE;
        });
        return Clutter.EVENT_STOP;
    }

    _rebuildMenu() {
        this._tooltip?.hide();
        this._countdownLabel = null;
        this.menu.removeAll();

        // Top Header Bar
        const headerItem = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
        });
        const headerBox = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL,
            style_class: 'pdfl-header-row',
            x_expand: true,
        });
        headerBox.add_child(new St.Label({
            text: 'Power Display',
            style_class: 'pdfl-header-title',
            y_align: Clutter.ActorAlign.CENTER,
            x_expand: true,
        }));

        if (this._state?.demo) {
            headerBox.add_child(new St.Label({
                text: 'Demo',
                style_class: 'pdfl-badge pdfl-badge-demo',
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }

        const linkedBtn = new St.Button({
            style_class: 'pdfl-icon-button',
            toggle_mode: true,
            checked: Boolean(this._state?.settings?.linked_brightness),
            can_focus: true,
            accessible_name: 'Toggle linked brightness',
            child: new St.Icon({
                icon_name: 'insert-link-symbolic',
                icon_size: 16,
            }),
        });
        linkedBtn.connect('clicked', () => {
            const next = !this._state?.settings?.linked_brightness;
            this._client.setLinkedModeAsync(next).catch(() => {});
        });
        this._tooltip.attach(
            linkedBtn,
            'Toggle linked brightness across all active displays');
        headerBox.add_child(linkedBtn);

        const rescanBtn = new St.Button({
            style_class: 'pdfl-icon-button',
            can_focus: true,
            accessible_name: 'Rescan monitors',
            child: new St.Icon({
                icon_name: 'view-refresh-symbolic',
                icon_size: 16,
            }),
        });
        rescanBtn.connect('clicked', () => {
            this._client.rescanAsync().catch(() => {});
        });
        this._tooltip.attach(rescanBtn, 'Rescan monitors and DDC/CI status');
        headerBox.add_child(rescanBtn);

        const settingsBtn = new St.Button({
            style_class: 'pdfl-icon-button',
            can_focus: true,
            accessible_name: 'Open settings',
            child: new St.Icon({
                icon_name: 'emblem-system-symbolic',
                icon_size: 16,
            }),
        });
        settingsBtn.connect('clicked', () => {
            this.menu.close();
            this._client.openSettingsAsync('displays').catch(() => {
                this._extension.openPreferences();
            });
        });
        this._tooltip.attach(settingsBtn, 'Open Power Display settings & profiles');
        headerBox.add_child(settingsBtn);

        headerItem.add_child(headerBox);
        this.menu.addMenuItem(headerItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        if (!this._client.isConnected) {
            const offlineItem = new PopupMenu.PopupMenuItem(
                'Background service (pdfl-daemon) inactive',
                {reactive: false});
            this.menu.addMenuItem(offlineItem);

            const retryItem = new PopupMenu.PopupMenuItem('Retry Connection');
            retryItem.connect('activate', () => {
                this._client.refreshStateAsync().catch(() => {});
            });
            this.menu.addMenuItem(retryItem);
            return;
        }

        if (!this._state) {
            this.menu.addMenuItem(
                new PopupMenu.PopupMenuItem('Loading monitors...', {reactive: false}));
            return;
        }

        // Pending rotation revert banner
        if (this._state.pending_rotation)
            this._addRotationRevertBanner(this._state.pending_rotation);

        // Inline confirmation banner (input switch / standby)
        if (this._pendingConfirmation)
            this._addInlineConfirmBanner(this._pendingConfirmation);

        // Warnings
        for (const warn of (this._state.warnings || [])) {
            const wItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            const lbl = new St.Label({
                text: warn,
                style_class: 'pdfl-error-label',
                x_expand: true,
            });
            lbl.clutter_text.line_wrap = true;
            wItem.add_child(lbl);
            this.menu.addMenuItem(wItem);
        }

        const monitors = (this._state.monitors || []).filter(m => !m.hidden);
        const prefsMap = this._state.settings?.monitor_prefs || {};

        // Linked Brightness Master Slider
        const writableCount = monitors.filter(
            m => m.capabilities?.brightness && m.capabilities?.brightness_writable).length;
        if (this._state.settings?.linked_brightness && writableCount > 0) {
            const masterVal = this._state.linked_brightness_value ?? 50;
            this._addSliderRow(
                this.menu,
                'display-brightness-symbolic',
                'All Monitors',
                masterVal,
                true,
                'linked:brightness',
                (val, immediate) => {
                    this._client.setLinkedBrightnessAsync(val, !immediate).catch(() => {});
                });
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        }

        // Scrollable Monitors Section
        if (monitors.length === 0) {
            this.menu.addMenuItem(
                new PopupMenu.PopupMenuItem('No visible active monitors.', {reactive: false}));
        } else {
            const scrollView = new St.ScrollView({
                style_class: 'pdfl-scroll',
                hscrollbar_policy: St.PolicyType.NEVER,
                vscrollbar_policy: St.PolicyType.AUTOMATIC,
                overlay_scrollbars: true,
                x_expand: true,
            });
            const listSection = new PopupMenu.PopupMenuSection();
            for (const mon of monitors) {
                const prefs = prefsMap[mon.monitor_id] || {};
                this._addMonitorControls(listSection, mon, prefs);
            }
            scrollView.set_child(listSection.actor);
            const scrollContainer = new PopupMenu.PopupBaseMenuItem({
                reactive: false,
                can_focus: false,
            });
            scrollContainer.add_child(scrollView);
            this.menu.addMenuItem(scrollContainer);
        }

        // Profiles Switcher
        const profiles = this._state.profiles || [];
        if (this._state.settings?.show_profile_switcher && profiles.length > 0) {
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            const profSub = new PopupMenu.PopupSubMenuMenuItem('Apply Profile');
            for (const prof of profiles) {
                const pItem = new PopupMenu.PopupMenuItem(prof.name);
                pItem.connect('activate', () => {
                    this._client.applyProfileAsync(String(prof.id)).catch(() => {});
                });
                profSub.menu.addMenuItem(pItem);
            }
            this.menu.addMenuItem(profSub);
        }
    }

    _addRotationRevertBanner(pending) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'pdfl-revert-banner',
            x_expand: true,
        });
        const label = new St.Label({
            text: `Orientation ${pending.target_degrees}°: revert in ${this._countdownRemaining}s?`,
            x_expand: true,
        });
        label.clutter_text.line_wrap = true;
        this._countdownLabel = label;
        box.add_child(label);

        const btnRow = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL,
            style_class: 'pdfl-button-row',
        });
        const keepBtn = new St.Button({
            label: 'Keep',
            style_class: 'pdfl-action-btn-primary',
            can_focus: true,
        });
        keepBtn.connect('clicked', () => {
            this._client.confirmRotationAsync(pending.token).catch(() => {});
        });
        const revertBtn = new St.Button({
            label: 'Revert',
            style_class: 'pdfl-action-btn',
            can_focus: true,
        });
        revertBtn.connect('clicked', () => {
            this._client.revertRotationAsync(pending.token).catch(() => {});
        });
        btnRow.add_child(keepBtn);
        btnRow.add_child(revertBtn);
        box.add_child(btnRow);
        item.add_child(box);
        this.menu.addMenuItem(item);
    }

    _addInlineConfirmBanner(conf) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'pdfl-confirm-box',
            x_expand: true,
        });
        const lbl = new St.Label({text: conf.title, x_expand: true});
        lbl.clutter_text.line_wrap = true;
        box.add_child(lbl);

        const btnRow = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL,
            style_class: 'pdfl-button-row',
        });
        const okBtn = new St.Button({
            label: 'Confirm',
            style_class: 'pdfl-action-btn-primary',
            can_focus: true,
        });
        okBtn.connect('clicked', () => {
            const cur = this._pendingConfirmation;
            this._pendingConfirmation = null;
            if (cur) {
                this._client.setFeatureAsync(
                    cur.monitorId, cur.feature, cur.value, true, false).catch(() => {});
            }
            this._rebuildMenu();
        });
        const cancelBtn = new St.Button({
            label: 'Cancel',
            style_class: 'pdfl-action-btn',
            can_focus: true,
        });
        cancelBtn.connect('clicked', () => {
            this._pendingConfirmation = null;
            this._rebuildMenu();
        });
        btnRow.add_child(okBtn);
        btnRow.add_child(cancelBtn);
        box.add_child(btnRow);
        item.add_child(box);
        this.menu.addMenuItem(item);
    }

    _addMonitorControls(section, mon, prefs) {
        const caps = mon.capabilities || {};

        // Monitor Title Card Header
        const titleItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const cardHeader = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL,
            style_class: 'pdfl-monitor-header',
            x_expand: true,
        });
        cardHeader.add_child(new St.Label({
            text: mon.display_name,
            style_class: 'pdfl-monitor-name',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        }));
        const isSoftware = !mon.is_builtin && String(mon.brightness_mode || '').startsWith('software_');
        const isInactiveBuiltin = mon.is_builtin && mon.connection_status === 'inactive';

        let badgeText = mon.is_builtin
            ? (isInactiveBuiltin ? 'Built-in · Lid Closed' : 'Built-in')
            : (mon.connector || 'External');
        let badgeClass = 'pdfl-badge';
        let badgeTooltip = '';

        if (isInactiveBuiltin) {
            badgeClass = 'pdfl-badge pdfl-badge-inactive';
            badgeTooltip = 'Built-in laptop panel is currently inactive (e.g. lid closed).';
        } else if (mon.is_builtin) {
            badgeTooltip = 'Built-in laptop panel via backlight driver.';
        } else if (isSoftware) {
            badgeText = `${badgeText} · Software`;
            badgeClass = 'pdfl-badge pdfl-badge-software';
            badgeTooltip = 'GPU software mode active (Mutter Gamma/CTM; DDC/CI unavailable on this port).';
        } else if (mon.ddc_status && mon.ddc_status !== 'available' && mon.ddc_status !== 'not_applicable') {
            badgeText = `${badgeText} · No DDC`;
            badgeTooltip = mon.last_error || 'Monitor does not respond over DDC/CI.';
        } else {
            badgeTooltip = 'Hardware control active via DDC/CI.';
        }

        const badgeLabel = new St.Label({
            text: badgeText,
            style_class: badgeClass,
            reactive: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._tooltip.attach(badgeLabel, badgeTooltip);
        cardHeader.add_child(badgeLabel);
        titleItem.add_child(cardHeader);
        section.addMenuItem(titleItem);

        if (mon.status_level === 'info' && mon.status_notice) {
            const infoItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            const infoLabel = new St.Label({
                text: mon.status_notice,
                style_class: 'pdfl-info-label',
                x_expand: true,
            });
            infoLabel.clutter_text.line_wrap = true;
            infoItem.add_child(infoLabel);
            section.addMenuItem(infoItem);
        } else if (mon.last_error) {
            const errItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            const errLabel = new St.Label({
                text: mon.last_error,
                style_class: 'pdfl-error-label',
                x_expand: true,
            });
            errLabel.clutter_text.line_wrap = true;
            errItem.add_child(errLabel);
            section.addMenuItem(errItem);
        }

        if (caps.brightness && mon.brightness !== null && mon.brightness !== undefined) {
            this._addSliderRow(
                section,
                'display-brightness-symbolic',
                `Brightness ${mon.display_name}`,
                mon.brightness,
                Boolean(caps.brightness_writable),
                `${mon.monitor_id}:brightness`,
                (val, immediate) => {
                    this._client.setFeatureAsync(
                        mon.monitor_id, 'brightness', val, true, !immediate).catch(() => {});
                },
                isSoftware
                    ? 'Software brightness via Mutter (minimum 35% safety floor against blackouts)'
                    : (caps.brightness_writable ? 'Adjust monitor brightness' : 'Brightness is currently read-only'));
        }

        if (caps.contrast && (prefs.show_contrast ?? true) && mon.contrast !== null) {
            this._addSliderRow(
                section,
                'preferences-color-symbolic',
                `Contrast ${mon.display_name}`,
                mon.contrast,
                true,
                `${mon.monitor_id}:contrast`,
                (val, immediate) => {
                    this._client.setFeatureAsync(
                        mon.monitor_id, 'contrast', val, true, !immediate).catch(() => {});
                },
                isSoftware
                    ? 'Software contrast via Mutter Gamma LUT (50% = neutral, no black crush)'
                    : 'Adjust monitor contrast (VCP 0x12)');
        }

        if (caps.volume && (prefs.show_volume ?? true) && mon.volume !== null) {
            this._addSliderRow(
                section,
                'audio-volume-medium-symbolic',
                `Volume ${mon.display_name}`,
                mon.volume,
                true,
                `${mon.monitor_id}:volume`,
                (val, immediate) => {
                    this._client.setFeatureAsync(
                        mon.monitor_id, 'volume', val, true, !immediate).catch(() => {});
                },
                'Adjust monitor speaker volume (VCP 0x62)');
        }

        if (caps.color_temperature && (isSoftware || (prefs.show_color_temperature ?? false))) {
            const ctLabel = isSoftware ? 'Color Temperature (Software)' : 'Color Temperature';
            const sub = new PopupMenu.PopupSubMenuMenuItem(ctLabel);
            this._tooltip.attach(
                sub,
                isSoftware
                    ? 'Select white point via Mutter Gamma LUT (6500K = neutral)'
                    : 'Select hardware color preset (VCP 0x14)');
            for (const preset of (mon.available_color_presets || [])) {
                const pItem = new PopupMenu.PopupMenuItem(preset.name);
                pItem.setOrnament(
                    mon.color_temperature === preset.value
                        ? PopupMenu.Ornament.DOT
                        : PopupMenu.Ornament.NONE);
                pItem.connect('activate', () => {
                    this._client.setFeatureAsync(
                        mon.monitor_id, 'color_temperature', preset.value, true, false).catch(() => {});
                });
                sub.menu.addMenuItem(pItem);
            }
            section.addMenuItem(sub);
        }

        if (isSoftware) {
            const resetItem = new PopupMenu.PopupMenuItem('Restore Defaults');
            this._tooltip.attach(
                resetItem,
                'Immediately resets brightness (100%), contrast (50%), and color temperature (6500K)');
            resetItem.connect('activate', () => {
                if (!this._client.isConnected)
                    return;
                const steps = [
                    () => this._client.setFeatureAsync(mon.monitor_id, 'brightness', 100, true, false),
                    ...(caps.contrast
                        ? [() => this._client.setFeatureAsync(mon.monitor_id, 'contrast', 50, true, false)]
                        : []),
                    ...(caps.color_temperature
                        ? [() => this._client.setFeatureAsync(mon.monitor_id, 'color_temperature', 0x05, true, false)]
                        : []),
                ];
                steps.reduce((p, fn) => p.then(fn), Promise.resolve()).catch(() => {});
            });
            section.addMenuItem(resetItem);
        }

        if (caps.input_source && (prefs.show_input_source ?? true)) {
            const sub = new PopupMenu.PopupSubMenuMenuItem('Switch Input');
            for (const inp of (mon.available_inputs || [])) {
                const iItem = new PopupMenu.PopupMenuItem(inp.name);
                iItem.setOrnament(
                    mon.input_source === inp.value
                        ? PopupMenu.Ornament.DOT
                        : PopupMenu.Ornament.NONE);
                iItem.connect('activate', () => {
                    this._pendingConfirmation = {
                        monitorId: mon.monitor_id,
                        feature: 'input_source',
                        value: inp.value,
                        title: `Really switch input on ${mon.display_name} to ${inp.name}?`,
                    };
                    this._rebuildMenu();
                });
                sub.menu.addMenuItem(iItem);
            }
            section.addMenuItem(sub);
        }

        if (caps.power_state && (prefs.show_power_state ?? false)) {
            const sub = new PopupMenu.PopupSubMenuMenuItem('Monitor Power');
            for (const pw of (mon.available_power_states || [])) {
                const pwItem = new PopupMenu.PopupMenuItem(pw.name);
                pwItem.setOrnament(
                    mon.power_state === pw.value
                        ? PopupMenu.Ornament.DOT
                        : PopupMenu.Ornament.NONE);
                pwItem.connect('activate', () => {
                    if (pw.value === 1) {
                        this._client.setFeatureAsync(
                            mon.monitor_id, 'power_state', 1, true, false).catch(() => {});
                        return;
                    }
                    this._pendingConfirmation = {
                        monitorId: mon.monitor_id,
                        feature: 'power_state',
                        value: pw.value,
                        title: `Really set ${mon.display_name} to '${pw.name}' mode?`,
                    };
                    this._rebuildMenu();
                });
                sub.menu.addMenuItem(pwItem);
            }
            section.addMenuItem(sub);
        }

        if (caps.rotation && (prefs.show_rotation ?? true)) {
            const sub = new PopupMenu.PopupSubMenuMenuItem(`Orientation (${mon.rotation ?? 0}°)`);
            for (const deg of ROTATION_CHOICES) {
                const rItem = new PopupMenu.PopupMenuItem(`${deg}°`);
                rItem.setOrnament(
                    mon.rotation === deg ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
                rItem.connect('activate', () => {
                    this._client.requestRotationAsync(mon.monitor_id, deg).catch(() => {});
                });
                sub.menu.addMenuItem(rItem);
            }
            section.addMenuItem(sub);
        }
    }

    _addSliderRow(container, iconName, accessibleName, initialPct, sensitive, key, onCommit, tooltipText = '') {
        const item = new PopupMenu.PopupBaseMenuItem({
            activate: false,
            can_focus: false,
        });
        const row = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL,
            style_class: 'pdfl-slider-row',
            x_expand: true,
        });
        const iconActor = new St.Icon({
            icon_name: iconName,
            style_class: 'pdfl-slider-icon',
            reactive: Boolean(tooltipText),
            y_align: Clutter.ActorAlign.CENTER,
        });
        if (tooltipText)
            this._tooltip.attach(iconActor, tooltipText);
        row.add_child(iconActor);

        const clampedInit = Math.max(0, Math.min(100, Number(initialPct) || 0));
        const slider = new Slider.Slider(clampedInit / 100.0);
        slider.accessible_name = accessibleName;
        slider.reactive = sensitive;
        slider.can_focus = sensitive;

        const valLabel = new St.Label({
            text: `${Math.round(clampedInit)}%`,
            style_class: 'pdfl-value-label',
            y_align: Clutter.ActorAlign.CENTER,
        });

        let dragging = false;
        this._syncingSliders = true;
        slider.value = clampedInit / 100.0;
        this._syncingSliders = false;

        slider.connect('drag-begin', () => {
            dragging = true;
        });
        slider.connect('drag-end', () => {
            dragging = false;
            const prevTimer = this._sliderTimers.get(key);
            if (prevTimer) {
                this._clearTimer(prevTimer);
                this._sliderTimers.delete(key);
            }
            const pct = Math.round(slider.value * 100);
            onCommit(pct, true);
        });
        slider.connect('notify::value', () => {
            if (this._syncingSliders)
                return;
            const pct = Math.round(slider.value * 100);
            valLabel.text = `${pct}%`;
            const prevTimer = this._sliderTimers.get(key);
            if (prevTimer)
                this._clearTimer(prevTimer);
            const tId = this._scheduleTimeout(dragging ? 90 : 50, () => {
                this._sliderTimers.delete(key);
                onCommit(pct, !dragging);
                return GLib.SOURCE_REMOVE;
            });
            this._sliderTimers.set(key, tId);
        });

        row.add_child(slider);
        row.add_child(valLabel);
        item.add_child(row);
        container.addMenuItem(item);
    }

    _onDestroy() {
        if (this._tooltip) {
            this._tooltip.destroy();
            this._tooltip = null;
        }
        for (const id of [...this._timers])
            GLib.source_remove(id);
        this._timers.clear();
        this._sliderTimers.clear();
        this._wheelTimerId = 0;
        this._countdownTimerId = 0;

        if (this._client && this._clientSignalIds) {
            for (const id of this._clientSignalIds)
                this._client.disconnect(id);
            this._clientSignalIds = [];
        }
        if (this._menuOpenId && this.menu) {
            this.menu.disconnect(this._menuOpenId);
            this._menuOpenId = 0;
        }
        if (this._scrollId) {
            this.disconnect(this._scrollId);
            this._scrollId = 0;
        }
        super._onDestroy();
    }
});

export default class PdflExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._client = new PdflDbusClient();
        this._indicator = new PdflPanelIndicator(this, this._client);
        this._quickIndicator = new PdflQuickBrightnessIndicator(this, this._client);
        this._quickIndicator.attachToQuickSettings();

        const box = this._settings.get_string('panel-box') || 'right';
        const pos = this._settings.get_int('panel-position');
        Main.panel.addToStatusArea(this.uuid, this._indicator, pos, box);

        Main.wm.addKeybinding(
            'toggle-flyout',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => {
                this._indicator?.menu?.toggle();
            });
    }

    disable() {
        Main.wm.removeKeybinding('toggle-flyout');

        if (this._quickIndicator) {
            this._quickIndicator.destroy();
            this._quickIndicator = null;
        }
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
        if (this._client) {
            this._client.destroy();
            this._client = null;
        }
        this._settings = null;
    }
}
