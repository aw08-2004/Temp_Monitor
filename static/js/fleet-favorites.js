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
    if (!dialog || !window.FleetApi || !FleetApi.machine) return;

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
            return (favorite.params.script || '').replace(/\s+/g, ' ').trim() || '(empty script)';
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
            sharedBadge.textContent = favorite.owned ? 'shared by you' : `shared by ${favorite.owner_email}`;
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
        actions.appendChild(button('Use', 'primary', () => {
            dialog.close();
            if (onPick) onPick(favorite);
        }));
        // Sharing grants read, not write -- the hub enforces this too (403), this just
        // avoids offering a button that would fail.
        if (favorite.owned) {
            actions.appendChild(button('Edit', 'ghost', () => renderForm(favorite)));
            actions.appendChild(button('Delete', 'ghost', async () => {
                if (!window.confirm(`Delete favorite "${favorite.name}"?`)) return;
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
        heading.textContent = `${title} (${favorites.length})`;
        group.appendChild(heading);
        for (const favorite of favorites) group.appendChild(renderRow(favorite));
        return group;
    }

    async function renderList() {
        reset('Favorites');
        const loading = document.createElement('div');
        loading.className = 'stat-card__meta';
        loading.textContent = 'Loading…';
        bodyEl.appendChild(loading);

        let favorites;
        try {
            favorites = await FleetApi.favorites.list();
        } catch (e) {
            bodyEl.textContent = '';
            showError(`Could not load favorites: ${e.message}`);
            return;
        }

        bodyEl.textContent = '';
        if (!favorites.length) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            empty.textContent = 'No favorites yet. Run something in the terminal, then ' +
                                'use "Save as favorite".';
            bodyEl.appendChild(empty);
        } else {
            const mine = favorites.filter((f) => f.owned);
            const team = favorites.filter((f) => !f.owned);
            if (mine.length) bodyEl.appendChild(renderGroup('Mine', mine));
            if (team.length) bodyEl.appendChild(renderGroup('Shared with me', team));
        }
        footEl.appendChild(button('Close', 'ghost', () => dialog.close()));
    }

    // ---------------- Create / edit ----------------
    function renderForm(existing) {
        reset(existing ? 'Edit favorite' : 'Save as favorite');

        const nameField = document.createElement('label');
        nameField.className = 'favorites__field';
        const nameLabel = document.createElement('span');
        nameLabel.className = 'favorites__field-label';
        nameLabel.textContent = 'Name';
        const nameInput = document.createElement('input');
        nameInput.className = 'input';
        nameInput.type = 'text';
        nameInput.value = existing ? existing.name : '';
        nameInput.placeholder = 'Fix printer spooler';
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
            scriptLabel.textContent = 'Script';
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
        sharedLabel.textContent = 'Share with the team (they can run it, only you can edit it)';
        sharedField.append(sharedInput, sharedLabel);
        bodyEl.appendChild(sharedField);

        async function save() {
            const payload = {
                name: nameInput.value.trim(),
                type,
                params: scriptInput
                    ? { ...params, script: scriptInput.value }
                    : params,
                shared: sharedInput.checked
            };
            try {
                if (existing) await FleetApi.favorites.update(existing.id, payload);
                else await FleetApi.favorites.create(payload);
                await renderList();
            } catch (e) {
                showError(e.message);   // duplicate name, blank name, bad type
            }
        }

        footEl.append(
            button('Cancel', 'ghost', () => (existing ? renderList() : dialog.close())),
            button(existing ? 'Save changes' : 'Save favorite', 'primary', save)
        );
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
