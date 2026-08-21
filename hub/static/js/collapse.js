// Remembers which sections an operator folded away.
//
// Modelled on tabs.js, down to the storage-key shape: one key per section, namespaced by
// page, so the Firmware fold on the Tools page and the one on the machine page are
// separate answers to separate questions.
//
// The restore works by setting the `open` attribute, NOT by any bespoke show/hide. That is
// the whole design: <details> fires `toggle` asynchronously after the attribute changes,
// so the poll-while-open logic that machine.js and processes.js already hang off `toggle`
// keeps working with no changes at all -- a section restored open starts polling by
// itself, exactly as if somebody had clicked it. Anything cleverer here would mean
// teaching every one of those files about a second way to become visible.
//
// Consequently this must load LAST: those modules register their `toggle` listeners during
// top-level execution, and a restore that ran first would fire into nothing.
(function () {
    'use strict';

    const STORAGE_PREFIX = 'tempmonitor:fold:';

    function read(key) {
        try { return localStorage.getItem(STORAGE_PREFIX + key); } catch (e) { return null; }
    }

    function write(key, open) {
        try { localStorage.setItem(STORAGE_PREFIX + key, open ? 'open' : 'closed'); }
        catch (e) { /* private mode */ }
    }

    function attach(details) {
        if (!details || details.dataset.foldBound === '1') return details;
        const key = details.dataset.foldKey;
        if (!key) return details;
        details.dataset.foldBound = '1';

        // Stored value wins; with nothing stored the markup decides, which is how a new
        // section ships open and Processes / All-sensors keep their closed default
        // without either being named here.
        const stored = read(key);
        if (stored === 'open') details.open = true;
        else if (stored === 'closed') details.open = false;

        details.addEventListener('toggle', () => write(key, details.open));
        return details;
    }

    window.Collapse = {
        /** Bind every unbound [data-fold-key] under `root`. Safe to call repeatedly. */
        init(root) {
            (root || document).querySelectorAll('details[data-fold-key]').forEach(attach);
        },
        /**
         * Bind one <details> built at runtime. Call it BEFORE inserting the element, so a
         * section restored closed never flashes open -- backup-tab.js rebuilds its whole
         * pane on every state change, and a flash per keystroke is what that would mean.
         */
        attach
    };

    document.addEventListener('DOMContentLoaded', () => window.Collapse.init());
})();
