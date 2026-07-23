// Shared fleet API surface, used by fleet-terminal.js and fleet-favorites.js. Loaded
// before both.
//
// A deliberate global, matching common.js: this codebase has no bundler and no module
// system, so `window.FleetApi` is the convention available. The alternative -- each
// module keeping its own getJson/formatTime/issueCommand -- is copies that drift.
(function () {
    'use strict';

    const machineConfig = document.getElementById('machine-config');
    // null on pages without a machine (dashboard/history). Consumers early-return on it,
    // matching the existing guard idiom.
    const MACHINE = machineConfig ? machineConfig.dataset.machine : null;

    async function getJson(url) {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`${url} returned ${response.status}`);
        return response.json();
    }

    // NOTE: the JSON content-type here is load-bearing beyond convenience. The hub only
    // reads application/json bodies, which is what stops a cross-site form POST from a
    // signed-in operator's browser from issuing fleet commands (commands are not signed;
    // the session is the only gate). See fleet_web.py's module docstring.
    async function sendJson(method, url, body) {
        const init = { method, headers: { 'Content-Type': 'application/json' } };
        if (body !== undefined) init.body = JSON.stringify(body);
        const response = await fetch(url, init);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `Hub returned ${response.status}.`);
        return data;
    }

    function postJson(url, body) {
        return sendJson('POST', url, body);
    }

    function formatTime(epochSeconds) {
        const value = Number(epochSeconds);
        if (!Number.isFinite(value)) return '--';
        return new Date(value * 1000).toLocaleString();
    }

    window.FleetApi = {
        machine: MACHINE,
        getJson,
        postJson,
        formatTime,

        /** Queue a command. Returns its id. */
        async issueCommand(type, params) {
            const data = await postJson('/api/fleet/commands', { machine: MACHINE, type, params });
            return data.command_id;
        },

        /** Chunks with seq > afterSeq, plus status/result. One request per poll tick. */
        fetchOutput(commandId, afterSeq) {
            return getJson(
                `/api/fleet/commands/${encodeURIComponent(commandId)}/output` +
                `?after_seq=${encodeURIComponent(afterSeq)}`);
        },

        // Saved commands/scripts. Ownership is always taken from the session server-side,
        // so there is deliberately no owner field to pass here.
        favorites: {
            list() {
                return getJson('/api/fleet/favorites');
            },
            create(favorite) {
                return postJson('/api/fleet/favorites', favorite);
            },
            update(id, favorite) {
                return sendJson('PUT', `/api/fleet/favorites/${encodeURIComponent(id)}`, favorite);
            },
            remove(id) {
                return sendJson('DELETE', `/api/fleet/favorites/${encodeURIComponent(id)}`);
            }
        }
    };
})();
