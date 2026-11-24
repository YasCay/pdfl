/**
 * Pure ESM view-model helpers for the GNOME Quick Settings BenQ brightness slider
 * and DDC/CI status row. Kept free of GI/GNOME imports so Node tests can import it directly.
 */

function isBenqMonitor(monitor) {
    if (!monitor)
        return false;
    const mfg = String(monitor.manufacturer || '').trim().toUpperCase();
    if (mfg === 'BNQ')
        return true;
    const label = `${monitor.display_name || ''} ${monitor.model || ''}`;
    return /\bbenq\b/i.test(label);
}

function hasWritableBrightness(monitor) {
    const caps = monitor?.capabilities || {};
    return Boolean(
        caps.brightness &&
        caps.brightness_writable &&
        monitor.brightness !== null &&
        monitor.brightness !== undefined
    );
}

function isSoftwareBrightness(monitor) {
    return String(monitor?.brightness_mode || '').startsWith('software_');
}

/**
 * Select the primary external monitor to feature in Quick Settings.
 * Prioritizes BenQ monitors (writable brightness, then primary, then first),
 * then primary external monitor, then any writable external monitor, then first external.
 */
export function selectQuickSettingsMonitor(state) {
    const externals = (state?.monitors || []).filter(m => !m.hidden && !m.is_builtin);
    if (externals.length === 0)
        return null;

    const benqMonitors = externals.filter(isBenqMonitor);
    if (benqMonitors.length > 0) {
        return (
            benqMonitors.find(hasWritableBrightness) ||
            benqMonitors.find(m => Boolean(m.is_primary)) ||
            benqMonitors[0]
        );
    }

    return (
        externals.find(m => Boolean(m.is_primary)) ||
        externals.find(hasWritableBrightness) ||
        externals[0]
    );
}

/**
 * Format a concise English DDC/CI status string for Quick Settings.
 */
export function formatDdcShortStatus(monitor) {
    if (!monitor)
        return 'Brightness unavailable';

    const status = monitor.ddc_status || 'not_applicable';
    let base;
    switch (status) {
    case 'ddc_unresponsive':
        base = 'No DDC/CI response';
        break;
    case 'bus_locked':
        base = 'DDC bus locked';
        break;
    case 'timeout':
        base = 'DDC/CI timed out';
        break;
    case 'permission_denied':
        base = 'No I2C access';
        break;
    case 'no_i2c_bus':
        base = 'No DDC/CI bus';
        break;
    case 'error':
        base = 'DDC/CI error';
        break;
    default:
        if (
            monitor.capabilities?.brightness &&
            !monitor.capabilities?.brightness_writable &&
            monitor.brightness !== null &&
            monitor.brightness !== undefined
        ) {
            base = `Brightness read-only (${Math.round(Number(monitor.brightness))}%)`;
        } else {
            base = 'Brightness unavailable';
        }
        break;
    }

    const connector = String(monitor.connector || '').trim();
    return connector ? `${base} (${connector})` : base;
}

/**
 * Build the Quick Settings view model from D-Bus connection state and daemon state.
 */
export function buildQuickBrightnessView({isConnected, state, inlineError = null}) {
    if (!isConnected) {
        return {
            mode: 'status',
            visible: true,
            monitorId: null,
            title: 'Power Display',
            brightness: null,
            shortStatus: 'Background daemon inactive',
            menuSubtitle: 'pdfl-daemon disconnected',
            statusLevel: 'error',
            infoText: null,
            errorText: 'The session service pdfl-daemon is currently not running.',
            detailText: 'The session service pdfl-daemon is currently not running.',
            actionLabel: 'Retry Connection',
            isSoftwareDimming: false,
            softwareFallbackEnabled: false,
            canToggleSoftwareFallback: false,
            hasContrast: false,
            contrast: null,
            hasColorTemperature: false,
            colorTemperature: null,
            colorPresets: [],
            externalMonitors: [],
        };
    }

    if (!state) {
        return {
            mode: 'status',
            visible: true,
            monitorId: null,
            title: 'Power Display',
            brightness: null,
            shortStatus: 'Loading monitors…',
            menuSubtitle: 'Waiting for status',
            statusLevel: 'info',
            infoText: 'Loading monitor status…',
            errorText: null,
            detailText: null,
            actionLabel: 'Rescan',
            isSoftwareDimming: false,
            softwareFallbackEnabled: false,
            canToggleSoftwareFallback: false,
            hasContrast: false,
            contrast: null,
            hasColorTemperature: false,
            colorTemperature: null,
            colorPresets: [],
            externalMonitors: [],
        };
    }

    const externals = (state.monitors || []).filter(m => !m.hidden && !m.is_builtin);
    const mon = selectQuickSettingsMonitor(state);
    const swEnabled = Boolean(state.settings?.software_dimming_fallback);
    if (!mon) {
        return {
            mode: 'hidden',
            visible: false,
            monitorId: null,
            title: 'Power Display',
            brightness: null,
            shortStatus: '',
            menuSubtitle: '',
            statusLevel: 'none',
            infoText: null,
            errorText: null,
            detailText: null,
            actionLabel: 'Rescan',
            isSoftwareDimming: false,
            softwareFallbackEnabled: swEnabled,
            canToggleSoftwareFallback: false,
            hasContrast: false,
            contrast: null,
            hasColorTemperature: false,
            colorTemperature: null,
            colorPresets: [],
            externalMonitors: [],
        };
    }

    const busPart = (mon.i2c_bus !== null && mon.i2c_bus !== undefined)
        ? `/dev/i2c-${mon.i2c_bus}`
        : null;
    const baseTitle = mon.display_name || 'External Monitor';
    const isSoftware = isSoftwareBrightness(mon);
    const canToggleSw = Boolean(mon.ddc_status && mon.ddc_status !== 'available' && mon.ddc_status !== 'not_applicable');
    const caps = mon.capabilities || {};
    const hasContrast = Boolean(caps.contrast && mon.contrast !== null && mon.contrast !== undefined);
    const contrast = hasContrast ? Math.max(0, Math.min(100, Math.round(Number(mon.contrast)))) : null;
    const colorPresets = Array.isArray(mon.available_color_presets) ? mon.available_color_presets : [];
    const hasColorTemperature = Boolean(caps.color_temperature && colorPresets.length > 0);
    const colorTemperature = hasColorTemperature && mon.color_temperature !== null && mon.color_temperature !== undefined
        ? Number(mon.color_temperature)
        : null;

    const externalMonitors = externals.map(m => ({
        monitorId: m.monitor_id,
        title: m.display_name || m.connector || m.monitor_id,
        connector: m.connector || '',
        writable: hasWritableBrightness(m),
        isSoftware: isSoftwareBrightness(m),
        brightness: hasWritableBrightness(m)
            ? Math.max(0, Math.min(100, Math.round(Number(m.brightness))))
            : null,
        shortStatus: hasWritableBrightness(m) ? '' : formatDdcShortStatus(m),
    }));

    if (hasWritableBrightness(mon)) {
        const menuSubtitle = isSoftware
            ? [mon.connector || null, 'Software mode (no DDC/CI)'].filter(Boolean).join(' · ')
            : [mon.connector || null, busPart].filter(Boolean).join(' · ');
        const infoText = isSoftware
            ? (mon.status_notice || 'Controlled in software mode (Mutter) — DDC/CI unavailable on this port.')
            : (mon.status_level === 'info' ? (mon.status_notice || null) : null);
        const errorText = inlineError || (!isSoftware && mon.status_level !== 'info' ? (mon.last_error || null) : null);
        return {
            mode: 'slider',
            visible: true,
            monitorId: mon.monitor_id,
            title: isSoftware ? `${baseTitle} · Software` : baseTitle,
            brightness: Math.max(0, Math.min(100, Math.round(Number(mon.brightness)))),
            shortStatus: '',
            menuSubtitle,
            statusLevel: errorText ? 'error' : (infoText ? 'info' : 'none'),
            infoText,
            errorText,
            detailText: errorText || infoText || null,
            actionLabel: 'Rescan Monitors',
            isSoftwareDimming: isSoftware,
            softwareFallbackEnabled: swEnabled,
            canToggleSoftwareFallback: canToggleSw,
            hasContrast,
            contrast,
            hasColorTemperature,
            colorTemperature,
            colorPresets,
            externalMonitors,
        };
    }

    const menuSubtitle = [mon.connector || null, busPart].filter(Boolean).join(' · ');
    const errorText = mon.last_error || inlineError || null;
    return {
        mode: 'status',
        visible: true,
        monitorId: mon.monitor_id,
        title: baseTitle,
        brightness: null,
        shortStatus: formatDdcShortStatus(mon),
        menuSubtitle,
        statusLevel: errorText ? 'error' : 'none',
        infoText: null,
        errorText,
        detailText: errorText,
        actionLabel: 'Re-check DDC/CI',
        isSoftwareDimming: false,
        softwareFallbackEnabled: swEnabled,
        canToggleSoftwareFallback: canToggleSw,
        hasContrast,
        contrast,
        hasColorTemperature,
        colorTemperature,
        colorPresets,
        externalMonitors,
    };
}
