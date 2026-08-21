// Lifecycle for a tool panel whose machine can change underneath it.
//
// Every tool module (backup, firmware, network, terminal) used to own three near-identical
// lines: find my panel, bail if it isn't there, load once on tab:shown. That was enough
// while the machine was fixed for the document's lifetime. It is not enough on the Tools
// page, where picking a different machine has to unwind whatever the panel was doing --
// stop its pollers, drop its state, clear what it drew -- and start again, without
// reloading the page.
//
// Rather than grow four copies of that unwinding (and four chances to leak a poll loop),
// the sequencing lives here and each module supplies only the two halves it can answer:
// how to load itself for a machine, and how to stop.
//
// Note the asymmetry between visible and hidden panels. A hidden panel is torn down and
// left unloaded; it reloads when it is next shown, because loading four panels on every
// machine click would fire four requests the operator did not ask for. Only the panel
// actually on screen reloads immediately.
(function () {
    'use strict';

    function machine() {
        return window.MachineContext ? window.MachineContext.current() : null;
    }

    function ensure(reg) {
        const target = machine();
        // requires() lets a panel decline to load (e.g. no machine picked yet) without
        // being marked loaded, so it retries the next time it is shown.
        if (reg.requires && !reg.requires(target)) return;
        if (reg.loaded && reg.loadedFor === target) return;
        reg.loaded = true;
        reg.loadedFor = target;
        reg.load(target);
    }

    function teardown(reg) {
        if (!reg.loaded) return;
        reg.loaded = false;
        reg.loadedFor = null;
        if (reg.teardown) reg.teardown();
    }

    window.ToolPanels = {
        /**
         * @param {string} name         for debugging only
         * @param {object} opts
         * @param {string} opts.panelId     the tab panel this tool draws into
         * @param {function} opts.load      (machine) => void; render for this machine
         * @param {function} [opts.teardown] stop timers, drop state, clear the panel
         * @param {function} [opts.requires] (machine) => bool; skip loading when false
         */
        register(name, opts) {
            const pane = document.getElementById(opts.panelId);
            // Absent when the capability gate never rendered the panel. Silent, matching
            // the `if (!pane) return;` guard this replaces.
            if (!pane) return null;

            const reg = {
                name,
                pane,
                load: opts.load,
                teardown: opts.teardown,
                requires: opts.requires,
                loaded: false,
                loadedFor: null
            };

            pane.addEventListener('tab:shown', () => ensure(reg));

            document.addEventListener('machine:changed', () => {
                teardown(reg);
                // Only the panel on screen reloads now; the rest wait for tab:shown.
                if (!reg.pane.hidden) ensure(reg);
            });

            // pagehide rather than beforeunload: beforeunload is not fired for a page
            // entering the back/forward cache, which would leave a poll loop running in a
            // page the operator believes they left.
            window.addEventListener('pagehide', () => teardown(reg));

            return {
                /** Drop the loaded flag so the next tab:shown re-runs load(). */
                invalidate() { reg.loaded = false; reg.loadedFor = null; },
                /** Load now if this panel is visible; used at boot by the owning page. */
                ensure() { ensure(reg); }
            };
        }
    };
})();
