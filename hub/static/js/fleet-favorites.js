// Favorites: saved commands/scripts, per operator, optionally shared with the team.
//
// Owns the page's <dialog> and exposes two entry points to the terminal:
//   FleetFavorites.open({ onPick })     -- browse/run/edit/delete
//   FleetFavorites.openSave({ type, params }) -- save what's currently typed
//
// Built on native <dialog>.showModal(): focus trapping, background inertness, Esc, and
// top-layer stacking come free. Favorite names and script text are user-authored and
// shared across the team, so like agent output they go in via textContent only.
(function () {
    'use strict';

    const dialog = document.getElementById('favorites-dialog');
    // Deliberately NOT gated on FleetApi.machine. Favorites are per-operator, and every
    // endpoint behind them is machine-independent -- the machine only matters at the
    // moment one is RUN, which the terminal handles. The Tools page has no machine at
    // parse time (one is picked later, without a reload), so a machine guard here meant
    // window.FleetFavorites was never defined and the button did nothing.
    if (!dialog || !window.FleetApi) return;

    const titleEl = document.getElementById('favorites-title');
    const bodyEl = document.getElementById('favorites-body');
    const footEl = document.getElementById('favorites-foot');
    const closeBtn = document.getElementById('favorites-close');

    let onPick = null;

    function reset(title) {
        titleEl.textContent = title;
        bodyEl.textContent = '';
        footEl.textContent = '';
    }

    function showError(message) {
        const existing = bodyEl.querySelector('.favorites__error');
        if (existing) existing.remove();
        const el = document.createElement('div');
        el.className = 'favorites__error';
        el.textContent = message;
        bodyEl.prepend(el);
    }

    function button(label, variant, onClick) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = variant ? `btn btn--${variant}` : 'btn';
        b.textContent = label;
        b.addEventListener('click', onClick);
        return b;
    }

    /** One-line summary of what a favorite actually does, for the list. */
    function preview(favorite) {
        if (favorite.command_type === 'run_script') {
            return (favorite.params.script || '').replace(/\s+/g, ' ').trim()
                   || t('machine.favorites.empty_script');
        }
        const params = JSON.stringify(favorite.params || {});
        return params === '{}' ? favorite.command_type : `${favorite.command_type} ${params}`;
    }

    // ---------------- Browse ----------------
    function renderRow(favorite) {
        const row = document.createElement('div');
        row.className = 'favorites__row';

        const main = document.createElement('div');
        main.className = 'favorites__row-main';

        const name = document.createElement('div');
        name.className = 'favorites__row-name';
        const nameText = document.createElement('span');
        nameText.textContent = favorite.name;          // user-authored
        name.appendChild(nameText);

        const typeBadge = document.createElement('span');
        typeBadge.className = 'badge';
        typeBadge.textContent = favorite.command_type;
        name.appendChild(typeBadge);

        if (favorite.shared) {
            const sharedBadge = document.createElement('span');
            sharedBadge.className = 'badge';
            // Who shared it matters when it's about to run as SYSTEM on your machine.
            sharedBadge.textContent = favorite.owned
                ? t('machine.favorites.shared_by_you')
                : t('machine.favorites.shared_by', { who: favorite.owner_email });
            name.appendChild(sharedBadge);
        }
        main.appendChild(name);

        const previewEl = document.createElement('div');
        previewEl.className = 'favorites__row-preview';
        previewEl.textContent = preview(favorite);     // user-authored
        previewEl.title = preview(favorite);
        main.appendChild(previewEl);
        row.appendChild(main);

        const actions = document.createElement('div');
        actions.className = 'favorites__row-actions';
        actions.appendChild(button(t('machine.favorites.use'), 'primary', () => {
            dialog.close();
            if (onPick) onPick(favorite);
        }));
        // Sharing grants read, not write -- the hub enforces this too (403), this just
        // avoids offering a button that would fail.
        if (favorite.owned) {
            actions.appendChild(button(t('common.edit'), 'ghost',
                                       () => renderForm(favorite)));
            actions.appendChild(button(t('common.delete'), 'ghost', async () => {
                if (!window.confirm(t('machine.favorites.confirm_delete',
                                      { name: favorite.name }))) return;
                try {
                    await FleetApi.favorites.remove(favorite.id);
                    await renderList();
                } catch (e) {
                    showError(e.message);
                }
            }));
        }
        row.appendChild(actions);
        return row;
    }

    function renderGroup(title, favorites) {
        const group = document.createElement('div');
        group.className = 'favorites__group';
        const heading = document.createElement('div');
        heading.className = 'favorites__group-title';
        heading.textContent = t('machine.favorites.group',
                                { title, count: favorites.length });
        group.appendChild(heading);
        for (const favorite of favorites) group.appendChild(renderRow(favorite));
        return group;
    }

    async function renderList() {
        reset(t('machine.favorites.title'));
        const loading = document.createElement('div');
        loading.className = 'stat-card__meta';
        loading.textContent = t('common.loading');
        bodyEl.appendChild(loading);

        let favorites;
        try {
            favorites = await FleetApi.favorites.list();
        } catch (e) {
            bodyEl.textContent = '';
            showError(t('machine.favorites.load_failed', { error: e.message }));
            return;
        }

        bodyEl.textContent = '';
        if (!favorites.length) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            empty.textContent = t('machine.favorites.empty');
            bodyEl.appendChild(empty);
        } else {
            const mine = favorites.filter((f) => f.owned);
            const team = favorites.filter((f) => !f.owned);
            if (mine.length) {
                bodyEl.appendChild(renderGroup(t('machine.favorites.mine'), mine));
            }
            if (team.length) {
                bodyEl.appendChild(renderGroup(t('machine.favorites.shared_with_me'), team));
            }
        }
        footEl.appendChild(button(t('common.close'), 'ghost', () => dialog.close()));
    }

    // ---------------- Create / edit ----------------
    function renderForm(existing) {
        reset(existing ? t('machine.favorites.edit_title')
                       : t('machine.favorites.save_title'));

        const nameField = document.createElement('label');
        nameField.className = 'favorites__field';
        const nameLabel = document.createElement('span');
        nameLabel.className = 'favorites__field-label';
        nameLabel.textContent = t('machine.favorites.name');
        const nameInput = document.createElement('input');
        nameInput.className = 'input';
        nameInput.type = 'text';
        nameInput.value = existing ? existing.name : '';
        nameInput.placeholder = t('machine.favorites.name_placeholder');
        nameField.append(nameLabel, nameInput);
        bodyEl.appendChild(nameField);

        const type = existing ? existing.command_type : pendingSave.type;
        const params = existing ? existing.params : pendingSave.params;

        let scriptInput = null;
        if (type === 'run_script') {
            const scriptField = document.createElement('label');
            scriptField.className = 'favorites__field';
            const scriptLabel = document.createElement('span');
            scriptLabel.className = 'favorites__field-label';
            scriptLabel.textContent = t('machine.favorites.script');
            scriptInput = document.createElement('textarea');
            scriptInput.className = 'input';
            scriptInput.spellcheck = false;
            scriptInput.value = params.script || '';
            scriptField.append(scriptLabel, scriptInput);
            bodyEl.appendChild(scriptField);
        }

        const sharedField = document.createElement('label');
        sharedField.className = 'favorites__checkbox';
        const sharedInput = document.createElement('input');
        sharedInput.type = 'checkbox';
        sharedInput.className = 'checkbox';
        sharedInput.checked = existing ? existing.shared : false;
        const sharedLabel = document.createElement('span');
        sharedLabel.textContent = t('machine.favorites.share');
        sharedField.append(sharedInput, sharedLabel);
        bodyEl.appendChild(sharedField);

        // Auto-save: no Save button. Same discipline as the permission-group editor --
        // saves are serialised, an edit made mid-request is written straight after, the
        // whole favorite goes every time, and nothing is created until there is a name.
        // From the first successful create we hold the id and update it in place, so a
        // second keystroke doesn't make a duplicate.
        let current = existing || null;
        let favSaving = false;
        let favPending = false;
        let favDebounce = null;

        const status = document.createElement('span');
        status.className = 'autosave';
        if (!current) status.textContent = t('machine.favorites.name_required');

        function setStatus(text, cls) {
            status.textContent = text;
            status.className = cls ? `autosave ${cls}` : 'autosave';
        }
        function scheduleSave() {           // text fields: debounce
            if (favDebounce) clearTimeout(favDebounce);
            favDebounce = setTimeout(autoSave, 500);
        }
        function autoSave() {               // discrete change, or the debounce firing
            if (favDebounce) { clearTimeout(favDebounce); favDebounce = null; }
            if (favSaving) { favPending = true; return; }
            flush();
        }
        async function flush() {
            const name = nameInput.value.trim();
            if (!current && !name) {
                setStatus(t('machine.favorites.name_required'), '');
                return;
            }
            const payload = {
                name,
                type,
                params: scriptInput ? { ...params, script: scriptInput.value } : params,
                shared: sharedInput.checked
            };
            favSaving = true;
            favPending = false;
            setStatus(t('common.saving'), '');
            try {
                if (current) {
                    await FleetApi.favorites.update(current.id, payload);
                } else {
                    const created = await FleetApi.favorites.create(payload);
                    current = { id: created.favorite_id };
                    titleEl.textContent = t('machine.favorites.edit_title');
                }
                setStatus(t('common.saved'), 'autosave--saved');
            } catch (e) {
                setStatus(e.message, 'autosave--error');   // duplicate name, blank name, bad type
            } finally {
                favSaving = false;
                if (favPending) flush();
            }
        }

        nameInput.addEventListener('input', scheduleSave);
        if (scriptInput) scriptInput.addEventListener('input', scheduleSave);
        sharedInput.addEventListener('change', autoSave);

        // "Done" just returns to the list -- the favorite is already saved. The dialog's
        // own header close button (favorites-close) still dismisses the whole thing.
        footEl.append(status, button(t('machine.favorites.done'), 'ghost',
                                    () => renderList()));
        nameInput.focus();
    }

    let pendingSave = { type: 'run_script', params: {} };

    // ---------------- Wiring ----------------
    closeBtn.addEventListener('click', () => dialog.close());
    // <dialog> sizes its backdrop to the whole viewport, so a click that lands on the
    // dialog element itself (rather than its contents) is a click on the backdrop.
    dialog.addEventListener('click', (e) => {
        if (e.target === dialog) dialog.close();
    });

    // showModal() BEFORE rendering: it moves focus to the first focusable descendant
    // (the close button), so anything focused during render would be overridden.
    window.FleetFavorites = {
        open(options) {
            onPick = (options && options.onPick) || null;
            dialog.showModal();
            renderList();
        },
        openSave(command) {
            pendingSave = { type: command.type, params: command.params || {} };
            dialog.showModal();
            renderForm(null);
        }
    };
})();
