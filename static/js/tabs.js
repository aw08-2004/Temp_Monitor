// Generic tab switcher. Drives any [role="tablist"] whose buttons carry
// aria-controls="<panel id>", so it isn't specific to the machine page.
//
// Follows the ARIA tabs pattern: exactly one tab is in the tab order at a time
// (roving tabindex) and arrows move between them, so Tab jumps past the whole strip to
// the panel content rather than walking every tab.
(function () {
    'use strict';

    const STORAGE_PREFIX = 'tempmonitor:tab:';

    function initTablist(tablist) {
        const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
        if (!tabs.length) return;

        const key = STORAGE_PREFIX + (tablist.dataset.tabsKey || 'default');
        const panelFor = (tab) => document.getElementById(tab.getAttribute('aria-controls'));

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

        // Restore, in priority order: an explicit #hash (so a tab is linkable), then the
        // last tab this browser used, else whatever the markup marked active.
        const fromHash = location.hash
            ? tabs.find((t) => panelFor(t) && `#${panelFor(t).id}` === location.hash)
            : null;
        let stored = null;
        try { stored = localStorage.getItem(key); } catch (e) { /* ignore */ }
        const fromStorage = stored ? tabs.find((t) => t.id === stored) : null;
        const initial = fromHash || fromStorage || tabs.find((t) => t.classList.contains('tabs__tab--active')) || tabs[0];
        activate(initial, { persist: false });
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[role="tablist"]').forEach(initTablist);
    });
})();
