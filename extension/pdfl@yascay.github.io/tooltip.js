import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

/**
 * Lightweight hover tooltip manager for GNOME Shell 50 St actors.
 */
export class HoverTooltip {
    constructor() {
        this._label = null;
        this._timerId = 0;
        this._activeActor = null;
        this._tracked = new Map(); // actor -> signalIds[]
    }

    _ensureLabel() {
        if (this._label)
            return this._label;
        this._label = new St.Label({
            style_class: 'pdfl-tooltip',
            visible: false,
            reactive: false,
        });
        this._label.clutter_text.line_wrap = true;
        Main.layoutManager.uiGroup.add_child(this._label);
        return this._label;
    }

    _clearTimer() {
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
    }

    attach(actor, text) {
        if (!actor || !text)
            return;
        this.detach(actor);

        const ids = [];
        ids.push(actor.connect('notify::hover', () => {
            if (actor.hover) {
                this._clearTimer();
                this._activeActor = actor;
                this._timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 320, () => {
                    this._timerId = 0;
                    if (this._activeActor === actor && actor.hover && actor.mapped)
                        this._showForActor(actor, text);
                    return GLib.SOURCE_REMOVE;
                });
            } else if (this._activeActor === actor) {
                this.hide();
            }
        }));

        if (actor instanceof St.Button) {
            ids.push(actor.connect('clicked', () => {
                if (this._activeActor === actor)
                    this.hide();
            }));
        }

        ids.push(actor.connect('destroy', () => {
            if (this._activeActor === actor)
                this.hide();
            this._tracked.delete(actor);
        }));

        this._tracked.set(actor, ids);
    }

    detach(actor) {
        const ids = this._tracked.get(actor);
        if (!ids)
            return;
        for (const id of ids) {
            try {
                actor.disconnect(id);
            } catch (_e) {
                // Ignore if actor already disposed
            }
        }
        this._tracked.delete(actor);
        if (this._activeActor === actor)
            this.hide();
    }

    _showForActor(actor, text) {
        const label = this._ensureLabel();
        label.text = text;
        label.set_position(0, 0);
        label.show();
        Main.layoutManager.uiGroup.set_child_above_sibling(label, null);

        const [ax, ay] = actor.get_transformed_position();
        const [aw, ah] = actor.get_transformed_size();
        const [, , lw, lh] = label.get_preferred_size();

        const monIdx = Main.layoutManager.findIndexForActor(actor);
        const mon = Main.layoutManager.monitors[monIdx] ?? Main.layoutManager.primaryMonitor;
        const pad = 8;

        let x = Math.round(ax + (aw - lw) / 2);
        let y = Math.round(ay + ah + 6);
        if (mon) {
            x = Math.max(mon.x + pad, Math.min(mon.x + mon.width - lw - pad, x));
            if (y + lh + pad > mon.y + mon.height)
                y = Math.max(mon.y + pad, Math.round(ay - lh - 6));
        }
        label.set_position(x, y);
    }

    hide() {
        this._clearTimer();
        this._activeActor = null;
        if (this._label)
            this._label.hide();
    }

    destroy() {
        this.hide();
        for (const actor of [...this._tracked.keys()])
            this.detach(actor);
        this._tracked.clear();
        if (this._label) {
            this._label.destroy();
            this._label = null;
        }
    }
}
