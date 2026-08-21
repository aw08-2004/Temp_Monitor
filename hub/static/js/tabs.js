// Generic tab switcher. Drives any [role="tablist"] whose buttons carry
// aria-controls="<panel id>", so it isn't specific to the machine page.
//
// Follows the ARIA tabs pattern: exactly one tab is in the tab order at a time
// (roving tabindex) and arrows move between them, so Tab jumps past the whole strip to
// the panel content rather than walking every tab.
//
// Two ways to be linkable, and a tablist opts into the second. By default the fragment
// names the panel (#tab-computer), which is what every in-page anchor in this console
// already does. A tablist carrying data-tabs-param="tab" instead reads and writes a query
// parameter against each tab's data-tab-slug, because the Tools page needs the tab AND the
// machine in one shareable URL and a fragment cannot carry two independent things.
(function () {
    'use strict';

    const STORAGE_PREFIX = 'tempmonitor:tab:';

    function initTablist(tablist) {
        const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
        if (!tabs.length) return;

        const key = STORAGE_PREFIX + (tablist.dataset.tabsKey || 'default');
        const panelFor = (tab) => document.getElementById(tab.getAttribute('aria-controls'));

        // Opt-in. Absent on every tablist that shipped before the Tools page, so their
        // behaviour is byte-for-byte what it was.
        const param = tablist.dataset.tabsParam || null;
        const tabForParam = () => {
            if (!param) return null;
            const wanted = new URLSearchParams(location.search).get(param);
            return wanted ? tabs.find((tb) => tb.dataset.tabSlug === wanted) : null;
        };
        function writeParam(tab) {
            if (!param || !tab.dataset.tabSlug) return;
            const url = new URL(location.href);
            if (url.searchParams.get(param) === tab.dataset.tabSlug) return;
            url.searchParams.set(param, tab.dataset.tabSlug);
            // Shared with the shell so the address bar and any bookmark agree; see
            // common.js for why this is replaceState on both sides.
            if (typeof syncUrl === 'function') syncUrl(url.pathname + url.search + url.hash);
        }

        function activate(tab, { focus = false, persist = true } = {}) {
            for (const other of tabs) {
                const selected = other === tab;
                other.classList.toggle('tabs__tab--active', selected);
                other.setAttribute('aria-selected', String(selected));
                // Roving tabindex: only the active tab is tabbable.
                other.tabIndex = selected ? 0 : -1;
                const panel = panelFor(other);
                if (panel) panel.hidden = !selected;
            }
            if (focus) tab.focus();
            if (persist) {
                try { localStorage.setItem(key, tab.id); } catch (e) { /* private mode */ }
            }
            // Let a panel react to becoming visible (e.g. a chart that must resize, or
            // the terminal focusing its prompt). Hidden elements have no dimensions, so
            // anything measuring itself has to wait for this.
            if (persist) writeParam(tab);
            const panel = panelFor(tab);
            if (panel) panel.dispatchEvent(new CustomEvent('tab:shown', { bubbles: true }));
        }

        tablist.addEventListener('click', (e) => {
            const tab = e.target.closest('[role="tab"]');
            if (tab) activate(tab);
        });

        tablist.addEventListener('keydown', (e) => {
            const current = tabs.indexOf(document.activeElement);
            if (current < 0) return;
            let next = null;
            // Both axes unconditionally, rather than plumbing an orientation flag: a
            // vertical tablist that ignores Up/Down is an accessibility bug (ARIA
            // specifies them for aria-orientation="vertical"), and on a horizontal strip
            // Up/Down previously did nothing, so accepting them costs no behaviour.
            if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = tabs[(current + 1) % tabs.length];
            else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = tabs[(current - 1 + tabs.length) % tabs.length];
            else if (e.key === 'Home') next = tabs[0];
            else if (e.key === 'End') next = tabs[tabs.length - 1];
            if (!next) return;
            e.preventDefault();
            activate(next, { focus: true });
        });

        const tabForHash = () => (location.hash
            ? tabs.find((t) => panelFor(t) && `#${panelFor(t).id}` === location.hash)
            : null);

        // Changing only the fragment is a same-document navigation: the page never
        // reloads, so the DOMContentLoaded restore below doesn't run again and a link to
        // #tab-data would do nothing when you're already on the page. Returns undefined
        // for a hash belonging to another tablist (or to no panel at all), which is why
        // this only acts on a match -- an unrelated in-page anchor must not steal a tab.
        window.addEventListener('hashchange', () => {
            const target = tabForHash();
            if (target) activate(target);
        });

        // The query-parameter equivalent of the hashchange handler above: the shell
        // navigates the frame with the History API, so arriving at a different ?tab= is
        // also a same-document change that DOMContentLoaded will not see again.
        if (param) {
            window.addEventListener('popstate', () => {
                const target = tabForParam();
                if (target) activate(target);
            });
        }

        // Restore, in priority order: an explicit ?tab= (opt-in, and it beats the fragment
        // because it is the form a link from another page carries), then an explicit #hash
        // (so a tab is linkable), then the last tab this browser used, else whatever the
        // markup marked active.
        const fromParam = tabForParam();
        const fromHash = tabForHash();
        let stored = null;
        try { stored = localStorage.getItem(key); } catch (e) { /* ignore */ }
        const fromStorage = stored ? tabs.find((t) => t.id === stored) : null;
        const initial = fromParam || fromHash || fromStorage || tabs.find((t) => t.classList.contains('tabs__tab--active')) || tabs[0];
        // persist:false skips the storage write on restore, but the URL must still be
        // brought into line -- a page opened with no ?tab= that restores from storage
        // should be sharable as the tab you are actually looking at.
        activate(initial, { persist: false });
        writeParam(initial);
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[role="tablist"]').forEach(initTablist);
    });
})();
