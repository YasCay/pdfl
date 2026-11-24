import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';

import {buildQuickBrightnessView} from './quickSettingsModel.js';
import {HoverTooltip} from './tooltip.js';

const PdflQuickBrightnessSlider = GObject.registerClass(
class PdflQuickBrightnessSlider extends QuickSettings.QuickSlider {
    _init(extension, client) {
        super._init({
            iconName: 'display-brightness-symbolic',
            iconReactive: true,
            iconLabel: 'Monitor Brightness',
            menuEnabled: true,
            menuButtonAccessibleName: 'Open monitor details',
        });

        this._extension = extension;
        this._client = client;
        this._state = null;
        this._inlineError = null;
        this._activeMonitorId = null;
        this._dragging = false;
        this._syncing = false;
        this._debounceTimerId = 0;
        this._contrastTimerId = 0;
        this._contrastDragging = false;
        this._pendingSubmenuRebuild = false;
        this._tooltip = new HoverTooltip();

        // Keep sliderBin inside sliderRow so GNOME 50's ATK parent chain stays intact.
        const sliderRow = this.get_child();
        this._sliderBin = this.slider.get_parent();
        sliderRow.add_style_class_name('pdfl-qs-slider-row');

        this._headerRow = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL,
            style_class: 'pdfl-qs-header',
            x_expand: true,
        });
        this._headerTitle = new St.Label({
            style_class: 'pdfl-qs-title',
            x_expand: true,
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._headerTitle.clutter_text.ellipsize = Pango.EllipsizeMode.END;

        this._headerValue = new St.Label({
            style_class: 'pdfl-qs-value',
            x_align: Clutter.ActorAlign.END,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._headerRow.add_child(this._headerTitle);
        this._headerRow.add_child(this._headerValue);

        this._statusBox = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'pdfl-qs-status-box',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
            visible: false,
        });
        this._statusTitle = new St.Label({
            style_class: 'pdfl-qs-status-title',
            x_expand: true,
            x_align: Clutter.ActorAlign.START,
        });
        this._statusTitle.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._statusSubtitle = new St.Label({
            style_class: 'pdfl-qs-status-subtitle',
            x_expand: true,
            x_align: Clutter.ActorAlign.START,
        });
        this._statusSubtitle.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._statusBox.add_child(this._statusTitle);
        this._statusBox.add_child(this._statusSubtitle);

        sliderRow.insert_child_below(this._statusBox, this._menuButton);

        this.set_child(null);
        this._rootBox = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'pdfl-qs-root',
            x_expand: true,
        });
        this._rootBox.add_child(this._headerRow);
        this._rootBox.add_child(sliderRow);
        this.set_child(this._rootBox);

        this._sliderSignalIds = [
            this.slider.connect('drag-begin', () => {
                this._dragging = true;
            }),
            this.slider.connect('drag-end', () => {
                this._dragging = false;
                this._clearDebounce();
                const pct = Math.round(this.slider.value * 100);
                this._headerValue.text = `${pct}%`;
                this._commitBrightness(pct, true);
            }),
            this.slider.connect('notify::value', () => {
                if (this._syncing)
                    return;
                const pct = Math.round(this.slider.value * 100);
                this._headerValue.text = `${pct}%`;
                this._clearDebounce();
                const delay = this._dragging ? 90 : 50;
                this._debounceTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                    this._debounceTimerId = 0;
                    this._commitBrightness(pct, !this._dragging);
                    return GLib.SOURCE_REMOVE;
                });
            }),
        ];

        this._iconClickedId = this.connect('icon-clicked', () => {
            this._tooltip?.hide();
            if (this.menuEnabled && this.menu)
                this.menu.open(true);
        });
        this._menuOpenChangedId = this.menu.connect('open-state-changed', (_m, isOpen) => {
            if (!isOpen)
                this._tooltip?.hide();
        });
        this._tooltip.attach(
            this._iconButton,
            'Open brightness details & picture controls');
        this._tooltip.attach(
            this._menuButton,
            'Open contrast, color temperature & status');

        this._clientSignalIds = [
            this._client.connect('state-changed', (_c, state) => {
                this._state = state;
                this._inlineError = null;
                this._sync();
            }),
            this._client.connect('connection-changed', () => {
                this._sync();
            }),
            this._client.connect('error-occurred', (_c, monitorId, message) => {
                if (!monitorId || monitorId === this._activeMonitorId)
                    this._inlineError = message;
                this._sync();
            }),
        ];

        this._sync();
    }

    _clearDebounce() {
        if (this._debounceTimerId) {
            GLib.source_remove(this._debounceTimerId);
            this._debounceTimerId = 0;
        }
    }

    _clearContrastTimer() {
        if (this._contrastTimerId) {
            GLib.source_remove(this._contrastTimerId);
            this._contrastTimerId = 0;
        }
    }

    _commitBrightness(pct, immediate) {
        if (!this._activeMonitorId || !this._client.isConnected)
            return;
        this._client.setFeatureAsync(
            this._activeMonitorId,
            'brightness',
            pct,
            true,
            !immediate
        ).catch(() => {});
    }

    _sync() {
        const view = buildQuickBrightnessView({
            isConnected: this._client.isConnected,
            state: this._state,
            inlineError: this._inlineError,
        });

        this.visible = view.visible;
        if (!view.visible) {
            this._activeMonitorId = null;
            return;
        }

        this._activeMonitorId = view.monitorId;

        if (view.mode === 'slider') {
            this._headerRow.visible = true;
            this._sliderBin.visible = true;
            this._statusBox.visible = false;

            this._headerTitle.text = view.title;
            this.slider.accessible_name = `Brightness ${view.title}`;

            if (!this._dragging) {
                const clamped = Math.max(0, Math.min(100, view.brightness));
                this._syncing = true;
                this.slider.value = clamped / 100.0;
                this._syncing = false;
                this._headerValue.text = `${clamped}%`;
            }
        } else {
            this._headerRow.visible = false;
            this._sliderBin.visible = false;
            this._statusBox.visible = true;

            this._statusTitle.text = view.title;
            this._statusSubtitle.text = view.shortStatus;
        }

        if (this._contrastDragging) {
            this._pendingSubmenuRebuild = true;
            return;
        }
        this._pendingSubmenuRebuild = false;
        this._rebuildSubmenu(view);
    }

    _rebuildSubmenu(view) {
        if (!this.menu)
            return;

        this._tooltip?.hide();
        this.menu.setHeader('display-brightness-symbolic', view.title, view.menuSubtitle || '');
        this.menu.removeAll();

        if (view.infoText) {
            const infoItem = new PopupMenu.PopupBaseMenuItem({
                reactive: false,
                can_focus: false,
            });
            const infoLabel = new St.Label({
                text: view.infoText,
                style_class: 'pdfl-qs-menu-info',
                x_expand: true,
            });
            infoLabel.clutter_text.line_wrap = true;
            infoItem.add_child(infoLabel);
            this.menu.addMenuItem(infoItem);
        }

        if (view.errorText) {
            const errItem = new PopupMenu.PopupBaseMenuItem({
                reactive: false,
                can_focus: false,
            });
            const errLabel = new St.Label({
                text: view.errorText,
                style_class: 'pdfl-qs-menu-error',
                x_expand: true,
            });
            errLabel.clutter_text.line_wrap = true;
            errItem.add_child(errLabel);
            this.menu.addMenuItem(errItem);
        }

        if (view.hasContrast && view.monitorId) {
            if (view.infoText || view.errorText)
                this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

            const cItem = new PopupMenu.PopupBaseMenuItem({
                activate: false,
                can_focus: false,
            });
            const cBox = new St.BoxLayout({
                orientation: Clutter.Orientation.VERTICAL,
                x_expand: true,
            });
            const cHead = new St.BoxLayout({
                orientation: Clutter.Orientation.HORIZONTAL,
                x_expand: true,
            });
            cHead.add_child(new St.Label({
                text: view.isSoftwareDimming ? 'Contrast (Software)' : 'Contrast',
                style_class: 'pdfl-qs-menu-section-title',
                x_expand: true,
                x_align: Clutter.ActorAlign.START,
            }));
            const cValLabel = new St.Label({
                text: `${view.contrast}%`,
                style_class: 'pdfl-qs-value',
                x_align: Clutter.ActorAlign.END,
            });
            cHead.add_child(cValLabel);
            cBox.add_child(cHead);

            const cRow = new St.BoxLayout({
                orientation: Clutter.Orientation.HORIZONTAL,
                style_class: 'pdfl-qs-menu-slider-row',
                x_expand: true,
            });
            const cIcon = new St.Icon({
                icon_name: 'preferences-color-symbolic',
                icon_size: 16,
                reactive: true,
                y_align: Clutter.ActorAlign.CENTER,
            });
            this._tooltip.attach(
                cIcon,
                view.isSoftwareDimming
                    ? 'Contrast curve via GPU Gamma LUT (50% = neutral, no black crush)'
                    : 'Hardware contrast (VCP 0x12)');
            cRow.add_child(cIcon);

            const cSlider = new Slider(Math.max(0, Math.min(100, view.contrast)) / 100.0);
            cSlider.accessible_name = `Contrast ${view.title}`;
            let syncingContrast = false;
            cSlider.connect('drag-begin', () => {
                this._contrastDragging = true;
            });
            cSlider.connect('drag-end', () => {
                this._contrastDragging = false;
                this._clearContrastTimer();
                const pct = Math.round(cSlider.value * 100);
                cValLabel.text = `${pct}%`;
                this._client.setFeatureAsync(
                    view.monitorId, 'contrast', pct, true, false).catch(() => {});
                if (this._pendingSubmenuRebuild) {
                    this._pendingSubmenuRebuild = false;
                    this._sync();
                }
            });
            cSlider.connect('notify::value', () => {
                if (syncingContrast)
                    return;
                const pct = Math.round(cSlider.value * 100);
                cValLabel.text = `${pct}%`;
                this._clearContrastTimer();
                const delay = this._contrastDragging ? 90 : 50;
                this._contrastTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                    this._contrastTimerId = 0;
                    this._client.setFeatureAsync(
                        view.monitorId, 'contrast', pct, true, this._contrastDragging).catch(() => {});
                    return GLib.SOURCE_REMOVE;
                });
            });
            syncingContrast = true;
            cSlider.value = Math.max(0, Math.min(100, view.contrast)) / 100.0;
            syncingContrast = false;

            const cBin = new St.Bin({
                style_class: 'slider-bin',
                child: cSlider,
                reactive: true,
                can_focus: true,
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            });
            cBin.connect('event', (_b, event) => cSlider.event(event, false));
            cRow.add_child(cBin);
            cBox.add_child(cRow);
            cItem.add_child(cBox);
            this.menu.addMenuItem(cItem);
        }

        if (view.hasColorTemperature && view.monitorId) {
            if (!view.hasContrast && (view.infoText || view.errorText))
                this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

            const ctItem = new PopupMenu.PopupBaseMenuItem({
                activate: false,
                can_focus: false,
            });
            const ctBox = new St.BoxLayout({
                orientation: Clutter.Orientation.VERTICAL,
                x_expand: true,
            });
            ctBox.add_child(new St.Label({
                text: view.isSoftwareDimming ? 'Color Temperature (Software)' : 'Color Temperature',
                style_class: 'pdfl-qs-menu-section-title',
                x_align: Clutter.ActorAlign.START,
            }));
            const presetRow = new St.BoxLayout({
                orientation: Clutter.Orientation.HORIZONTAL,
                style_class: 'pdfl-qs-preset-row',
                x_expand: true,
            });
            for (const preset of view.colorPresets) {
                const btn = new St.Button({
                    label: preset.name,
                    style_class: 'pdfl-qs-preset-btn',
                    toggle_mode: true,
                    checked: Number(view.colorTemperature) === Number(preset.value),
                    can_focus: true,
                    x_expand: true,
                });
                this._tooltip.attach(
                    btn,
                    view.isSoftwareDimming
                        ? `Set color temperature ${preset.name} via GPU (6500K = neutral)`
                        : `Monitor color preset ${preset.name} (VCP 0x14)`);
                btn.connect('clicked', () => {
                    this._client.setFeatureAsync(
                        view.monitorId, 'color_temperature', Number(preset.value), true, false
                    ).catch(() => {});
                });
                presetRow.add_child(btn);
            }
            ctBox.add_child(presetRow);
            ctItem.add_child(ctBox);
            this.menu.addMenuItem(ctItem);
        }

        if (view.isSoftwareDimming && view.monitorId) {
            const resetItem = new PopupMenu.PopupMenuItem('Restore Defaults');
            this._tooltip.attach(
                resetItem,
                'Immediately resets brightness (100%), contrast (50%), and color temperature (6500K)');
            resetItem.connect('activate', () => {
                if (!this._client.isConnected || !view.monitorId)
                    return;
                const steps = [
                    () => this._client.setFeatureAsync(view.monitorId, 'brightness', 100, true, false),
                    ...(view.hasContrast
                        ? [() => this._client.setFeatureAsync(view.monitorId, 'contrast', 50, true, false)]
                        : []),
                    ...(view.hasColorTemperature
                        ? [() => this._client.setFeatureAsync(view.monitorId, 'color_temperature', 0x05, true, false)]
                        : []),
                ];
                steps.reduce((p, fn) => p.then(fn), Promise.resolve()).catch(() => {});
            });
            this.menu.addMenuItem(resetItem);
        }

        const otherExternals = view.externalMonitors.filter(m => m.monitorId !== view.monitorId);
        if (otherExternals.length > 0) {
            if (view.infoText || view.errorText || view.hasContrast || view.hasColorTemperature)
                this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            for (const other of otherExternals) {
                const summary = other.writable
                    ? `${other.title}: ${other.brightness}%`
                    : `${other.title}: ${other.shortStatus}`;
                this.menu.addMenuItem(new PopupMenu.PopupMenuItem(summary, {reactive: false}));
            }
        }

        if (
            view.infoText ||
            view.errorText ||
            view.hasContrast ||
            view.hasColorTemperature ||
            otherExternals.length > 0
        )
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        if (view.canToggleSoftwareFallback) {
            const toggleLabel = view.softwareFallbackEnabled
                ? 'Disable Software Dimming'
                : 'Enable Software Dimming (without DDC/CI)';
            const swItem = new PopupMenu.PopupMenuItem(toggleLabel);
            swItem.connect('activate', () => {
                if (!this._client.isConnected)
                    return;
                const next = !view.softwareFallbackEnabled;
                const curSettings = this._state?.settings || {};
                this._client.saveSettingsAsync({
                    ...curSettings,
                    software_dimming_fallback: next,
                }).then(() => this._client.refreshStateAsync()).catch(() => {});
            });
            this.menu.addMenuItem(swItem);
        }

        const actionItem = new PopupMenu.PopupMenuItem(view.actionLabel);
        actionItem.connect('activate', () => {
            if (this._client.isConnected)
                this._client.rescanAsync().catch(() => {});
            else
                this._client.refreshStateAsync().catch(() => {});
        });
        this.menu.addMenuItem(actionItem);

        const settingsItem = new PopupMenu.PopupMenuItem('Power Display Settings');
        settingsItem.connect('activate', () => {
            Main.panel.statusArea.quickSettings?.menu?.close();
            this._client.openSettingsAsync('displays').catch(() => {
                this._extension.openPreferences();
            });
        });
        this.menu.addMenuItem(settingsItem);
    }

    destroy() {
        this._clearDebounce();
        this._clearContrastTimer();

        if (this._tooltip) {
            this._tooltip.destroy();
            this._tooltip = null;
        }
        if (this.menu && this._menuOpenChangedId) {
            this.menu.disconnect(this._menuOpenChangedId);
            this._menuOpenChangedId = 0;
        }

        if (this.slider && this._sliderSignalIds) {
            for (const id of this._sliderSignalIds)
                this.slider.disconnect(id);
            this._sliderSignalIds = [];
        }
        if (this._iconClickedId) {
            this.disconnect(this._iconClickedId);
            this._iconClickedId = 0;
        }
        if (this._client && this._clientSignalIds) {
            for (const id of this._clientSignalIds)
                this._client.disconnect(id);
            this._clientSignalIds = [];
        }
        if (this.menu) {
            this.menu.close(false);
            this._menuManager?.removeMenu(this.menu);
            this.menu.destroy();
            this.menu = null;
        }

        super.destroy();
    }
});

export const PdflQuickBrightnessIndicator = GObject.registerClass(
class PdflQuickBrightnessIndicator extends QuickSettings.SystemIndicator {
    _init(extension, client) {
        super._init();

        this._client = client;
        this._qsMenu = null;
        this._qsMenuOpenId = 0;
        this._qsGrid = null;
        this._qsGridChildAddedId = 0;
        this._repositionIdleId = 0;

        this._sliderItem = new PdflQuickBrightnessSlider(extension, client);
        this.quickSettingsItems.push(this._sliderItem);
    }

    _positionBelowVolume() {
        const quickSettings = Main.panel.statusArea.quickSettings;
        const grid = quickSettings?.menu?._grid;
        if (!grid || !this._sliderItem || this._sliderItem.get_parent() !== grid)
            return;

        const brightnessItem = quickSettings._brightness?.quickSettingsItems?.[0];
        const volumeAnchor =
            quickSettings._volumeInput?.quickSettingsItems?.at(-1) ??
            quickSettings._volumeOutput?.quickSettingsItems?.at(-1);

        if (brightnessItem && brightnessItem.get_parent() === grid) {
            if (this._sliderItem.get_next_sibling() !== brightnessItem)
                grid.set_child_below_sibling(this._sliderItem, brightnessItem);
        } else if (volumeAnchor && volumeAnchor.get_parent() === grid) {
            if (volumeAnchor.get_next_sibling() !== this._sliderItem)
                grid.set_child_above_sibling(this._sliderItem, volumeAnchor);
        }
    }

    _scheduleReposition() {
        if (this._repositionIdleId)
            return;
        this._repositionIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._repositionIdleId = 0;
            this._positionBelowVolume();
            return GLib.SOURCE_REMOVE;
        });
    }

    attachToQuickSettings() {
        const quickSettings = Main.panel.statusArea.quickSettings;
        if (!quickSettings)
            return;

        // Registers item with colSpan=2 and adds item.menu.actor to _overlay.
        quickSettings.addExternalIndicator(this, 2);
        this._positionBelowVolume();

        const grid = quickSettings.menu?._grid;
        if (grid) {
            this._qsGrid = grid;
            this._qsGridChildAddedId = grid.connect('child-added', () => {
                this._scheduleReposition();
            });
        }

        if (quickSettings.menu) {
            this._qsMenu = quickSettings.menu;
            this._qsMenuOpenId = this._qsMenu.connect('open-state-changed', (_m, isOpen) => {
                if (isOpen) {
                    this._positionBelowVolume();
                    if (this._client.isConnected)
                        this._client.refreshStateAsync().catch(() => {});
                }
            });
        }
    }

    destroy() {
        if (this._repositionIdleId) {
            GLib.source_remove(this._repositionIdleId);
            this._repositionIdleId = 0;
        }
        if (this._qsGrid && this._qsGridChildAddedId) {
            this._qsGrid.disconnect(this._qsGridChildAddedId);
            this._qsGridChildAddedId = 0;
        }
        this._qsGrid = null;

        if (this._qsMenu && this._qsMenuOpenId) {
            this._qsMenu.disconnect(this._qsMenuOpenId);
            this._qsMenuOpenId = 0;
        }
        this._qsMenu = null;

        for (const item of [...this.quickSettingsItems])
            item.destroy();
        this.quickSettingsItems = [];
        this._sliderItem = null;

        super.destroy();
    }
});
