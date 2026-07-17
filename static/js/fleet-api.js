// Shared fleet API surface + a tiny event bus, used by fleet-commands.js and
// fleet-terminal.js. Loaded before both.
//
// A deliberate global, matching common.js: this codebase has no bundler and no module
// system, so `window.FleetApi` is the convention available. The alternative -- each
// module keeping its own getJson/formatTime/issueCommand -- is three copies that drift.
//
// The event bus is how the terminal and the command panel share state without sharing
// variables: the terminal issues a command and emits; the panel refreshes its history
// and speeds up its poll. Neither imports the other, and load order beyond "this file
// first" doesn't matter.
(function () {
    'use strict';

    const machineConfig = document.getElementById('machine-config');
    // null on pages without a machine (dashboard/history). Consumers early-return on it,
    // matching the existing guard idiom.
    const MACHINE = machineConfig ? machineConfig.dataset.machine : null;

    const listeners = [];

    async function getJson(url) {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`${url} returned ${response.status}`);
        return response.json();
    }

    // NOTE: the JSON content-type here is load-bearing beyond convenience. The hub only
    // reads application/json bodies, which is what stops a cross-site form POST from a
    // signed-in operator's browser from issuing fleet commands (commands are not signed;
    // the session is the only gate). See fleet_web.py's module docstring.
    async function postJson(url, body) {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `Hub returned ${response.status}.`);
        return data;
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
            emitCommandIssued({ type, params, commandId: data.command_id });
            return data.command_id;
        },

        /** Chunks with seq > afterSeq, plus status/result. One request per poll tick. */
        fetchOutput(commandId, afterSeq) {
            return getJson(
                `/api/fleet/commands/${encodeURIComponent(commandId)}/output` +
                `?after_seq=${encodeURIComponent(afterSeq)}`);
        },

        listCommands(machine) {
            return getJson(`/api/fleet/commands?machine=${encodeURIComponent(machine)}`);
        },

        getCommand(commandId) {
            return getJson(`/api/fleet/commands/${encodeURIComponent(commandId)}`);
        },

        agentStatus() {
            return getJson('/api/fleet/status');
        },

        /** Subscribe to "a command was issued from this page". */
        onCommandIssued(callback) {
            listeners.push(callback);
        }
    };

    function emitCommandIssued(detail) {
        for (const callback of listeners) {
            // One bad subscriber must not stop the others, nor fail the issuing call --
            // the command is already queued by this point.
            try { callback(detail); } catch (e) { console.error('onCommandIssued handler failed', e); }
        }
    }
})();
