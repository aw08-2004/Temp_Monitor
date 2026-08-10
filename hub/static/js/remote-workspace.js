// The Remote page: several PCs open at once, one tab per screen.
//
// WHY THIS PAGE EXISTS. Remote view has always lived on the machine page, which is about one
// PC -- so watching two machines meant two browser tabs, and the moment you switched to one
// the other was a page you could no longer see the state of. Working on two at once is
// ordinary helpdesk work (read the error on the user's PC, fix it on the server it talks to),
// and nothing below the UI was ever in the way: sessions are keyed by machine, each runs on
// its own agent, and remote.py serialises nothing. See remote.js for the factory this drives.
//
// EVERY OPEN SCREEN STAYS CONNECTED, including the ones in background tabs. That is the whole
// point -- switching tabs is instant because there is nothing to reconnect, and an installer
// running on the PC you are not looking at keeps running where you can come back to it. It
// also means each open screen costs real bandwidth, which is why there is a cap (MAX_SCREENS)
// and why remote.js slows its signaling poll right down once media is flowing.
//
// LEAVING THE PAGE NO LONGER ENDS THEM EITHER. A WebRTC session belongs to the document that
// negotiated it, which used to mean that going to Packages tore every screen down. It does not
// any more: the app shell keeps this document alive in a frame while other pages load in
// another one (see static/js/shell.js -- /remote is the page it is built around), so a trip to
// Packages and back finds the same screens still connected.
//
// STILL NOT PERSISTED ACROSS RELOADS, and that part cannot be fixed here. Reload and the peer
// connection is gone whatever the shell does, so a restored tab would be a name with a dead
// session behind it. Reloading therefore ends every screen (see pagehide in remote.js) and
// leaves an empty strip, rather than pretending otherwise.
//
// IIFE-wrapped, classic script, no bundler -- same as every other file in static/js.
(function () {
    'use strict';

    const stripEl = document.getElementById('remote-tabs');
    const screensEl = document.getElementById('remote-screens');
    const addBtn = document.getElementById('remote-add');
    const emptyEl = document.getElementById('remote-empty');
    const template = document.getElementById('remote-viewer-template');
    const dialog = document.getElementById('remote-picker');
    const pickerSearch = document.getElementById('remote-picker-search');
    const pickerList = document.getElementById('remote-picker-list');
    const pickerCancel = document.getElementById('remote-picker-cancel');
    const pickerError = document.getElementById('remote-picker-error');

    // Everything, not just the container: this file is loaded on one page and needs all of
    // it, so a missing piece means the markup changed underneath it -- and finding that out
    // here beats a null dereference three clicks later, inside a session it half-opened.
    if (!window.RemoteViewer || !window.FleetApi) return;
    if ([stripEl, screensEl, addBtn, emptyEl, template, dialog,
         pickerSearch, pickerList, pickerCancel, pickerError].some((el) => !el)) return;

    // A ceiling on live screens, not a hub limit -- the hub happily runs more. Each screen is
    // a video stream being decoded in a browser tab, so this is about the OPERATOR's machine
    // and link, and four already means four desktops' worth of frames arriving at once.
    const MAX_SCREENS = 4;

    /** machine -> { machine, viewer, tabEl, labelEl, dotEl, panelEl }. Insertion order is
     *  tab order, which is what a Map preserves. */
    const screens = new Map();
    let activeMachine = null;

    // ---------------- The strip ----------------
    function makeTab(machine) {
        const tab = document.createElement('div');
        tab.className = 'screen-tab';
        tab.dataset.machine = machine;

        const select = document.createElement('button');
        select.type = 'button';
        select.className = 'screen-tab__select';
        select.setAttribute('role', 'tab');
        select.title = t('remote.tab_title', { machine });
        select.addEventListener('click', () => activate(machine));

        const dot = document.createElement('span');
        dot.className = 'screen-tab__dot screen-tab__dot--muted';

        const label = document.createElement('span');
        label.className = 'screen-tab__label';
        label.textContent = machine;

        select.append(dot, label);

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'screen-tab__close';
        close.setAttribute('aria-label', t('remote.close_screen', { machine }));
        close.title = t('remote.close_screen_title');
        close.textContent = '×';
        close.addEventListener('click', (e) => {
            e.stopPropagation();   // the tab's own click would just re-select it
            closeScreen(machine);
        });

        // Middle-click closes, as it does in every browser and terminal app. `auxclick` is
        // the event that actually fires for a non-primary button, and the mousedown default
        // has to be suppressed or the browser starts autoscroll instead.
        tab.addEventListener('mousedown', (e) => {
            if (e.button === 1) e.preventDefault();
        });
        tab.addEventListener('auxclick', (e) => {
            if (e.button !== 1) return;
            e.preventDefault();
            closeScreen(machine);
        });

        tab.append(select, close);
        stripEl.insertBefore(tab, addBtn);
        return { tabEl: tab, labelEl: label, dotEl: dot, selectEl: select };
    }

    function renderStrip() {
        for (const screen of screens.values()) {
            const active = screen.machine === activeMachine;
            screen.tabEl.classList.toggle('screen-tab--active', active);
            screen.selectEl.setAttribute('aria-selected', active ? 'true' : 'false');
        }
        addBtn.disabled = screens.size >= MAX_SCREENS;
        addBtn.title = addBtn.disabled
            ? t('remote.max_screens', { max: MAX_SCREENS })
            : t('remote.add_screen_title');
        emptyEl.hidden = screens.size > 0;
    }

    // ---------------- Opening, selecting, closing ----------------
    function activate(machine) {
        const screen = screens.get(machine);
        if (!screen) return;
        activeMachine = machine;
        for (const other of screens.values()) {
            other.panelEl.hidden = other.machine !== machine;
        }
        renderStrip();
        // Hand the keyboard to the desktop that just came to the front, so typing goes to the
        // PC you are looking at without clicking its picture first.
        screen.viewer.focus();
    }

    function openScreen(machine) {
        if (screens.has(machine)) { activate(machine); return; }
        if (screens.size >= MAX_SCREENS) return;

        const panel = document.createElement('div');
        panel.className = 'remote-screen';
        panel.append(template.content.cloneNode(true));
        screensEl.appendChild(panel);

        const tab = makeTab(machine);
        const screen = Object.assign({ machine, panelEl: panel }, tab);
        screens.set(machine, screen);

        // autoStart: picking a PC out of the dialog IS the decision to connect to it. A tab
        // that opened onto a black rectangle with a Start button would be a second click that
        // asks nothing.
        screen.viewer = window.RemoteViewer.create(
            panel.querySelector('[data-remote-viewer]'), machine, {
                autoStart: true,
                onStatus: (kind) => {
                    screen.dotEl.className = 'screen-tab__dot screen-tab__dot--' + kind;
                },
            });
        screen.viewer.setTitle(machine);
        activate(machine);
    }

    /** The × on a tab. Ends the session on that PC and drops the screen -- no confirmation,
     *  because the operator asked and a modal in front of every close only trains people to
     *  click through it. */
    function closeScreen(machine) {
        const screen = screens.get(machine);
        if (!screen) return;
        // Work out the neighbour to fall back to while this tab is still in the strip.
        const order = [...screens.keys()];
        const at = order.indexOf(machine);
        const next = order[at + 1] || order[at - 1] || null;

        screen.viewer.dispose();
        screen.tabEl.remove();
        screen.panelEl.remove();
        screens.delete(machine);

        if (activeMachine !== machine) renderStrip();
        else if (next) activate(next);
        else { activeMachine = null; renderStrip(); }
    }

    // ---------------- The machine picker ----------------
    // Every PC you could open, listed, one click each. No dropdown to open first and no
    // confirm button after: the click IS the choice, and seeing the whole list is the point.

    /** [{ machine, online }], online first then alphabetical -- an offline PC cannot be
     *  connected to, so it belongs at the bottom rather than salted through the list. */
    let pickable = [];

    function renderPickerList() {
        const q = pickerSearch.value.trim().toLowerCase();
        const rows = q ? pickable.filter((p) => p.machine.toLowerCase().includes(q)) : pickable;
        pickerList.replaceChildren();
        if (!rows.length) {
            const none = document.createElement('p');
            none.className = 'stat-card__meta';
            none.textContent = t('remote.picker_no_matches');
            pickerList.appendChild(none);
            return;
        }
        for (const row of rows) {
            // A <button>, not a styled <div>: Enter/Space, focus and the click itself all
            // come from the element rather than from event handlers reimplementing them.
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'picker-list__item';
            item.setAttribute('role', 'listitem');
            item.dataset.machine = row.machine;

            const pill = document.createElement('span');
            pill.className = 'status-pill';
            setStatusPill(pill, row.online ? 'ok' : 'muted',
                          row.online ? t('common.status.online') : t('common.status.offline'));

            const name = document.createElement('span');
            name.className = 'picker-list__name';
            name.textContent = row.machine;

            item.append(name, pill);
            item.addEventListener('click', () => {
                dialog.close();
                openScreen(row.machine);
            });
            pickerList.appendChild(item);
        }
    }

    async function openPicker() {
        pickerError.hidden = true;
        pickerSearch.value = '';
        pickerList.replaceChildren();
        pickable = [];
        dialog.showModal();
        let machines;
        try {
            machines = await window.FleetApi.getJson('/api/machines');
        } catch (e) {
            pickerError.textContent = t('remote.picker_failed', { error: e.message });
            pickerError.hidden = false;
            return;
        }
        // Already-open PCs are left out rather than shown and refused: the list answers
        // "which one next", and its own answer should always be openable.
        pickable = machines
            .filter((row) => row.machine && !screens.has(row.machine))
            .map((row) => ({ machine: row.machine, online: row.status === 'online' }))
            .sort((a, b) => (a.online === b.online)
                ? a.machine.localeCompare(b.machine)
                : (a.online ? -1 : 1));
        if (!pickable.length) {
            pickerError.textContent = machines.length
                ? t('remote.picker_all_open') : t('remote.picker_none');
            pickerError.hidden = false;
            return;
        }
        renderPickerList();
        // The list is already on screen; focus goes to the filter so a big fleet can be
        // typed down to one row without reaching for the mouse first.
        pickerSearch.focus();
    }

    // ---------------- Wiring ----------------
    addBtn.addEventListener('click', openPicker);
    pickerCancel.addEventListener('click', () => dialog.close());
    pickerSearch.addEventListener('input', renderPickerList);
    // Enter in the filter opens the only remaining match. With several left it would be a
    // guess at which one, so it does nothing and the click decides -- and the dialog's form
    // is method="dialog", so the key must be swallowed either way or it just closes.
    pickerSearch.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') {
            const first = pickerList.querySelector('.picker-list__item');
            if (first) { e.preventDefault(); first.focus(); }
            return;
        }
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const items = pickerList.querySelectorAll('.picker-list__item');
        if (items.length === 1) items[0].click();
    });

    // Arrow keys walk the list; typing anywhere in it goes back to the filter, so a fleet can
    // be narrowed without tabbing back up to the box.
    pickerList.addEventListener('keydown', (e) => {
        const items = [...pickerList.querySelectorAll('.picker-list__item')];
        const at = items.indexOf(document.activeElement);
        if (e.key === 'ArrowDown' && at > -1 && at < items.length - 1) {
            e.preventDefault();
            items[at + 1].focus();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            (at > 0 ? items[at - 1] : pickerSearch).focus();
        } else if ((e.key === 'Enter' || e.key === ' ') && at > -1) {
            // Handled rather than left to the button's own key activation. preventDefault
            // is what keeps that from ALSO firing, so the row opens exactly once -- and it
            // must come before the printable-character branch below, or Space would be
            // typed into the filter instead of opening the focused PC.
            e.preventDefault();
            items[at].click();
        } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
            pickerSearch.focus();       // the keystroke itself lands in the box
        }
    });

    // Arrow keys across the strip, matching the ARIA tabs pattern the page-level tabs follow
    // (see tabs.js) -- the tabs here are built at runtime, so they cannot use it directly.
    stripEl.addEventListener('keydown', (e) => {
        const order = [...screens.keys()];
        const at = order.indexOf(activeMachine);
        if (at < 0 || !order.length) return;
        let next = null;
        if (e.key === 'ArrowRight') next = order[(at + 1) % order.length];
        else if (e.key === 'ArrowLeft') next = order[(at - 1 + order.length) % order.length];
        else if (e.key === 'Home') next = order[0];
        else if (e.key === 'End') next = order[order.length - 1];
        if (!next) return;
        e.preventDefault();
        activate(next);
        screens.get(next).selectEl.focus();
    });

    renderStrip();

    // ?machine=NAME (repeatable) opens screens on arrival, so the page can be linked to with
    // the PCs you want already on it. Capped like everything else, and unknown names simply
    // fail to connect the same way a picked one would.
    const wanted = new URLSearchParams(location.search).getAll('machine');
    for (const name of wanted.slice(0, MAX_SCREENS)) {
        if (name) openScreen(name);
    }
})();
