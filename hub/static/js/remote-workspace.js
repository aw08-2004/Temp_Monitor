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
    const pickerSelect = document.getElementById('remote-picker-machine');
    const pickerOpen = document.getElementById('remote-picker-open');
    const pickerCancel = document.getElementById('remote-picker-cancel');
    const pickerError = document.getElementById('remote-picker-error');

    // Everything, not just the container: this file is loaded on one page and needs all of
    // it, so a missing piece means the markup changed underneath it -- and finding that out
    // here beats a null dereference three clicks later, inside a session it half-opened.
    if (!window.RemoteViewer || !window.FleetApi) return;
    if ([stripEl, screensEl, addBtn, emptyEl, template, dialog,
         pickerSelect, pickerOpen, pickerCancel, pickerError].some((el) => !el)) return;

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
    async function openPicker() {
        pickerError.hidden = true;
        pickerSelect.innerHTML = '';
        pickerOpen.disabled = true;
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
        const available = machines
            .map((row) => row.machine)
            .filter((name) => name && !screens.has(name));
        if (!available.length) {
            pickerError.textContent = machines.length
                ? t('remote.picker_all_open') : t('remote.picker_none');
            pickerError.hidden = false;
            return;
        }
        // A <select> shows its first option as the chosen one, so the dialog used to open
        // already pointing at whichever machine sorted first -- press Open without reading
        // it and you connect to a PC you never picked. A disabled placeholder makes "no
        // choice yet" a state the control can actually be in. Disabled also keeps it out of
        // the searchable combobox's list (autocomplete.js skips disabled options), so it is
        // a prompt rather than something pickable.
        const placeholder = new Option(t('remote.picker_placeholder'), '');
        placeholder.disabled = true;
        placeholder.selected = true;
        pickerSelect.appendChild(placeholder);
        for (const name of available) {
            pickerSelect.appendChild(new Option(name, name));
        }
        // Open stays disabled until something is actually picked.
        pickerOpen.disabled = true;
        // autocomplete.js watches the document and turns a long <select> into a searchable
        // combobox on its own, so a fleet of hundreds is typed at rather than scrolled. When
        // it has, the native select is aria-hidden with tabindex=-1 and focusing it would
        // drop the keystrokes on the floor -- the search box is the thing to type into.
        const search = pickerSelect.parentNode
            && pickerSelect.parentNode.querySelector('.select-search__input');
        (search || pickerSelect).focus();
    }

    function confirmPicker() {
        const machine = pickerSelect.value;
        if (!machine) return;             // still on the placeholder
        dialog.close();
        openScreen(machine);
    }

    // ---------------- Wiring ----------------
    addBtn.addEventListener('click', openPicker);
    pickerOpen.addEventListener('click', confirmPicker);
    pickerCancel.addEventListener('click', () => dialog.close());
    // Enter in the picker opens the selection rather than submitting nothing: the dialog's
    // form is method="dialog", so without this the key would just close it.
    pickerSelect.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' || pickerOpen.disabled) return;
        e.preventDefault();
        confirmPicker();
    });

    // Picking a machine connects to it -- the Open button is a fallback, not a second step.
    //
    // Which `change` counts as a pick is the whole difficulty. A NATIVE <select> fires
    // `change` on every arrow key while it is closed, so connecting on any change would
    // launch a session on each host the caret passed over. The two cases separate cleanly:
    //   * an untrusted `change` was dispatched by autocomplete.js's combobox, which emits it
    //     only on a deliberate pick (a click, or Enter on the highlighted row) -- always a
    //     choice, and note that merely arrowing its list emits nothing;
    //   * a trusted `change` came from the native control, where only a pointer means "this
    //     one" -- keyboard users commit with Enter, which is handled above.
    let pointerPick = false;
    pickerSelect.addEventListener('mousedown', () => { pointerPick = true; });
    pickerSelect.addEventListener('keydown', () => { pointerPick = false; });
    pickerSelect.addEventListener('change', (e) => {
        pickerOpen.disabled = !pickerSelect.value;
        const deliberate = !e.isTrusted || pointerPick;
        pointerPick = false;
        if (pickerSelect.value && deliberate) confirmPicker();
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
