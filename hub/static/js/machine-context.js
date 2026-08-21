// Which machine is the current page about, and a way to be told when that changes.
//
// A deliberate global, matching common.js and fleet-api.js: no bundler, no module system.
//
// This exists because the machine used to be a property of the DOCUMENT -- every tool
// module read `#machine-config`'s data-machine once, at parse time, and could never be
// told otherwise. That was true while the tools lived inside /machine/<name>. On the
// Tools page the operator picks a machine from a list without the page reloading, so the
// answer has to be a question you can ask again, and one that can announce its own change.
//
// Pages with a #machine-config (the machine page) seed from it and never call set(), so
// they behave exactly as they did. Pages without one start at null -- the same value
// remote.html has always seen -- and set() theirs at runtime.
(function () {
    'use strict';

    const configEl = document.getElementById('machine-config');
    let current = configEl ? (configEl.dataset.machine || null) : null;

    window.MachineContext = {
        /** The machine this page is currently about, or null if none is chosen yet. */
        current() {
            return current;
        },

        /**
         * Point the page at a different machine. No-ops when unchanged, so callers can
         * be careless about re-selecting the same row.
         *
         * The value is updated BEFORE the event is dispatched, so a listener that calls
         * current() sees the new machine rather than the one being left.
         */
        set(machine) {
            const next = machine || null;
            if (next === current) return;
            const previous = current;
            current = next;
            document.dispatchEvent(new CustomEvent('machine:changed', {
                detail: { machine: next, previous }
            }));
        },

        /** The #machine-config element, for the other data-* the machine page hangs off it. */
        config() {
            return configEl;
        },

        /** Sugar over the event, for readability at the call site. */
        onChange(fn) {
            document.addEventListener('machine:changed', (e) => fn(e.detail.machine, e.detail.previous));
        }
    };
})();
