import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as Signals from 'resource:///org/gnome/shell/misc/signals.js';

const BUS_NAME = 'io.github.yascay.pdfl.Daemon';
const OBJ_PATH = '/io/github/yascay/pdfl/Daemon';
const IFACE_NAME = 'io.github.yascay.pdfl.Daemon';

export class PdflDbusClient extends Signals.EventEmitter {
    constructor() {
        super();
        this._proxy = null;
        this._cancellable = new Gio.Cancellable();
        this._signalId = 0;
        this._ownerId = 0;
        this._destroyed = false;
        this._initProxyAsync();
    }

    get isConnected() {
        return Boolean(this._proxy && this._proxy.get_name_owner());
    }

    _initProxyAsync() {
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            null,
            BUS_NAME,
            OBJ_PATH,
            IFACE_NAME,
            this._cancellable,
            (_source, res) => {
                if (this._destroyed)
                    return;
                try {
                    const proxy = Gio.DBusProxy.new_for_bus_finish(res);
                    this._proxy = proxy;
                    this._ownerId = proxy.connect('notify::g-name-owner', () => {
                        this.emit('connection-changed', this.isConnected);
                        if (this.isConnected)
                            this.refreshStateAsync().catch(() => {});
                    });
                    this._signalId = proxy.connect(
                        'g-signal',
                        (_p, _sender, signalName, params) => {
                            this._onSignal(signalName, params);
                        });
                    this.emit('connection-changed', this.isConnected);
                    if (this.isConnected)
                        this.refreshStateAsync().catch(() => {});
                } catch (e) {
                    if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        this.emit('connection-changed', false);
                }
            });
    }

    _onSignal(signalName, params) {
        if (this._destroyed)
            return;
        try {
            if (signalName === 'StateChanged') {
                const [jsonStr] = params.deepUnpack();
                this.emit('state-changed', JSON.parse(jsonStr));
            } else if (signalName === 'ErrorOccurred') {
                const [monitorId, message] = params.deepUnpack();
                this.emit('error-occurred', monitorId, message);
            } else if (signalName === 'RotationPending') {
                const [jsonStr] = params.deepUnpack();
                this.emit('rotation-pending', JSON.parse(jsonStr));
            }
        } catch (_e) {
            // Ignore malformed D-Bus signal payloads
        }
    }

    _callAsync(methodName, variant = null, timeoutMs = 10000) {
        if (this._destroyed || !this._proxy || !this.isConnected)
            return Promise.reject(new Error('pdfl-daemon not connected'));

        return new Promise((resolve, reject) => {
            this._proxy.call(
                methodName,
                variant,
                Gio.DBusCallFlags.NONE,
                timeoutMs,
                this._cancellable,
                (proxy, res) => {
                    if (this._destroyed) {
                        reject(new Error('Client destroyed'));
                        return;
                    }
                    try {
                        const out = proxy.call_finish(res);
                        const unpacked = out.deepUnpack();
                        resolve(unpacked.length === 1 ? unpacked[0] : unpacked);
                    } catch (err) {
                        reject(err);
                    }
                });
        });
    }

    async refreshStateAsync() {
        const jsonStr = await this._callAsync('GetState');
        const state = JSON.parse(jsonStr);
        this.emit('state-changed', state);
        return state;
    }

    async rescanAsync() {
        const jsonStr = await this._callAsync('Rescan', null, 15000);
        const state = JSON.parse(jsonStr);
        this.emit('state-changed', state);
        return state;
    }

    async setFeatureAsync(monitorId, feature, value, confirmed = false, debounce = false) {
        const jsonStr = await this._callAsync(
            'SetFeature',
            new GLib.Variant('(ssibb)', [
                monitorId,
                feature,
                Math.round(value),
                Boolean(confirmed),
                Boolean(debounce),
            ]));
        return JSON.parse(jsonStr);
    }

    async setLinkedModeAsync(enabled) {
        const jsonStr = await this._callAsync(
            'SetLinkedMode',
            new GLib.Variant('(b)', [Boolean(enabled)]));
        const state = JSON.parse(jsonStr);
        this.emit('state-changed', state);
        return state;
    }

    async setLinkedBrightnessAsync(percent, debounce = false) {
        const jsonStr = await this._callAsync(
            'SetLinkedBrightness',
            new GLib.Variant('(ib)', [Math.round(percent), Boolean(debounce)]));
        return JSON.parse(jsonStr);
    }

    async stepBrightnessAsync(delta, modeOverride = '') {
        const jsonStr = await this._callAsync(
            'StepBrightness',
            new GLib.Variant('(is)', [Math.round(delta), modeOverride]));
        return JSON.parse(jsonStr);
    }

    async applyProfileAsync(profileId) {
        const jsonStr = await this._callAsync(
            'ApplyProfile',
            new GLib.Variant('(s)', [String(profileId)]));
        return JSON.parse(jsonStr);
    }

    async requestRotationAsync(monitorId, degrees) {
        const jsonStr = await this._callAsync(
            'RequestRotation',
            new GLib.Variant('(si)', [monitorId, Math.round(degrees)]));
        return JSON.parse(jsonStr);
    }

    async confirmRotationAsync(token) {
        const jsonStr = await this._callAsync(
            'ConfirmRotation',
            new GLib.Variant('(s)', [String(token)]));
        return JSON.parse(jsonStr);
    }

    async revertRotationAsync(token) {
        const jsonStr = await this._callAsync(
            'RevertRotation',
            new GLib.Variant('(s)', [String(token)]));
        return JSON.parse(jsonStr);
    }

    async openSettingsAsync(page = 'displays') {
        return Boolean(
            await this._callAsync('OpenSettings', new GLib.Variant('(s)', [page])));
    }

    async getSettingsAsync() {
        const jsonStr = await this._callAsync('GetSettings');
        return JSON.parse(jsonStr);
    }

    async saveSettingsAsync(settingsObj) {
        const jsonStr = await this._callAsync(
            'SaveSettings',
            new GLib.Variant('(s)', [JSON.stringify(settingsObj)]));
        return JSON.parse(jsonStr);
    }

    destroy() {
        this._destroyed = true;
        if (this._cancellable) {
            this._cancellable.cancel();
            this._cancellable = null;
        }
        if (this._proxy) {
            if (this._signalId) {
                this._proxy.disconnect(this._signalId);
                this._signalId = 0;
            }
            if (this._ownerId) {
                this._proxy.disconnect(this._ownerId);
                this._ownerId = 0;
            }
            this._proxy = null;
        }
    }
}
