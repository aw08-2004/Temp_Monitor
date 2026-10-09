// The Recordings page (roadmap #19): the caller's own session recordings and the ones shared
// with them. Everything it shows comes from GET /api/recordings, already narrowed server-side
// to what this person may see (recordings_web.py) -- nothing here decides access, it only
// leaves off buttons the hub would refuse: Share and Delete exist on owned rows only, because
// only the owner may share (no re-sharing) or delete.
//
// Every confirmation is the console's own dialog, never the browser's.
(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const mineBody = $('recordings-mine').querySelector('tbody');
    const sharedBody = $('recordings-shared').querySelector('tbody');
    const player = $('recordings-player');
    const video = $('recordings-video');
    const shareDialog = $('recordings-share');
    const errorLine = $('recordings-error');

    let groupNames = new Map();
    let allGroups = [];
    let sharing = null;     // the recording the share dialog is editing

    function showError(message) {
        errorLine.textContent = message || '';
        errorLine.hidden = !message;
    }

    function formatBytes(bytes) {
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let value = Number(bytes) || 0;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
        return (unit === 0 ? value : value.toFixed(1)) + ' ' + units[unit];
    }

    function formatDuration(seconds) {
        const total = Math.max(0, Math.floor(seconds || 0));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = String(total % 60).padStart(2, '0');
        return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    }

    // Why a recording ended (a copy of remote-recorder.js's, which this page does not load), one LITERAL key per code: the i18n test only sees literal t()
    // calls, and tests/test_recordings_web.py checks this list against recordings.END_REASONS
    // so a reason added on the hub cannot reach the console untranslated.
    const END_REASON_TEXT = {
        stopped: () => t('recordings.end_reason.stopped'),
        time_limit: () => t('recordings.end_reason.time_limit'),
        session_ended: () => t('recordings.end_reason.session_ended'),
        helper_restarted: () => t('recordings.end_reason.helper_restarted'),
        badge_failed: () => t('recordings.end_reason.badge_failed'),
        badge_timeout: () => t('recordings.end_reason.badge_timeout'),
        stream_changed: () => t('recordings.end_reason.stream_changed'),
        size_limit: () => t('recordings.end_reason.size_limit'),
    };

    function endReasonText(code) {
        return (END_REASON_TEXT[code] || END_REASON_TEXT.stopped)();
    }

    // Literal keys for the same reason as END_REASON_TEXT.
    const STATUS_TEXT = {
        starting: () => t('recordings.status.starting'),
        recording: () => t('recordings.status.recording'),
        ended: () => t('recordings.status.ended'),
        failed: () => t('recordings.status.failed'),
    };

    function statusText(rec) {
        if (rec.status === 'ended' && rec.end_reason && rec.end_reason !== 'stopped') {
            return t('recordings.status.ended_because', { reason: endReasonText(rec.end_reason) });
        }
        return (STATUS_TEXT[rec.status] || STATUS_TEXT.failed)();
    }

    function sharedSummary(rec) {
        const shares = rec.shares || { groups: [], users: [] };
        const names = shares.groups.map((id) => groupNames.get(id) || id).concat(shares.users);
        return names.length ? names.join(', ') : t('recordings.not_shared');
    }

    function button(label, className, onClick) {
        const b = el('button', className, label);
        b.type = 'button';
        b.addEventListener('click', onClick);
        return b;
    }

    function videoUrl(rec, download) {
        return `/api/recordings/${encodeURIComponent(rec.id)}/video` + (download ? '?download=1' : '');
    }

    function playable(rec) {
        return rec.status === 'ended' && rec.size_bytes > 0;
    }

    function viewActions(rec) {
        const cell = el('td', 'data-table__actions');
        if (playable(rec)) {
            cell.appendChild(button(t('recordings.play'), 'btn', () => play(rec)));
            // A plain link, so the browser's own download handles a file this size; the hub
            // writes the audit row when it serves it.
            const link = el('a', 'btn', t('recordings.download'));
            link.href = videoUrl(rec, true);
            link.setAttribute('download', '');
            cell.appendChild(link);
        }
        return cell;
    }

    function renderMine(rows) {
        mineBody.replaceChildren();
        $('recordings-mine-empty').hidden = rows.length > 0;
        rows.forEach((rec) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, fmtTime(rec.created_at)));
            tr.appendChild(el('td', null, rec.machine));
            tr.appendChild(el('td', null, formatDuration(rec.duration_seconds)));
            tr.appendChild(el('td', null, formatBytes(rec.size_bytes)));
            tr.appendChild(el('td', null, rec.reason));
            tr.appendChild(el('td', null, statusText(rec)));
            tr.appendChild(el('td', null, sharedSummary(rec)));
            const actions = viewActions(rec);
            actions.appendChild(button(t('recordings.share'), 'btn', () => openShare(rec)));
            actions.appendChild(button(t('common.delete'), 'btn btn--danger', () => openDelete(rec)));
            tr.appendChild(actions);
            mineBody.appendChild(tr);
        });
    }

    function renderShared(rows) {
        sharedBody.replaceChildren();
        $('recordings-shared-empty').hidden = rows.length > 0;
        rows.forEach((rec) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, fmtTime(rec.created_at)));
            tr.appendChild(el('td', null, rec.machine));
            tr.appendChild(el('td', null, rec.owner));
            tr.appendChild(el('td', null, formatDuration(rec.duration_seconds)));
            tr.appendChild(el('td', null, rec.reason));
            tr.appendChild(viewActions(rec));
            sharedBody.appendChild(tr);
        });
    }

    async function load() {
        try {
            const data = await (async () => {
                const response = await fetch('/api/recordings');
                if (!response.ok) throw new Error(t('common.hub_error', { status: response.status }));
                return response.json();
            })();
            allGroups = data.groups || [];
            groupNames = new Map(allGroups.map((g) => [g.id, g.name]));
            $('recordings-usage').textContent = tPlural('recordings.usage', data.usage.count, {
                size: formatBytes(data.usage.bytes),
            });
            renderMine(data.owned || []);
            renderShared(data.shared || []);
            showError('');
        } catch (e) {
            showError(e.message);
        }
    }

    // ---- player ----------------------------------------------------------------------
    function play(rec) {
        $('recordings-player-title').textContent =
            t('recordings.player_title', { machine: rec.machine, time: fmtTime(rec.created_at) });
        $('recordings-player-reason').textContent = rec.reason;
        video.src = videoUrl(rec, false);
        player.showModal();
        const started = video.play();
        if (started?.catch) started.catch(() => {});
    }

    function closePlayer() {
        video.pause();
        video.removeAttribute('src');
        video.load();
        if (player.open) player.close();
    }

    $('recordings-player-close').addEventListener('click', closePlayer);
    player.addEventListener('close', closePlayer);

    // ---- sharing ---------------------------------------------------------------------
    function openShare(rec) {
        sharing = rec;
        const shares = rec.shares || { groups: [], users: [] };
        const host = $('recordings-share-groups');
        host.replaceChildren();
        if (!allGroups.length) host.appendChild(el('p', 'stat-card__meta', t('recordings.no_groups')));
        allGroups.forEach((group) => {
            const label = el('label', 'setting__label');
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.value = group.id;
            box.checked = shares.groups.includes(group.id);
            label.append(box, ' ' + group.name);
            host.appendChild(label);
        });
        $('recordings-share-users').value = shares.users.join('\n');
        $('recordings-share-error').hidden = true;
        shareDialog.showModal();
    }

    async function saveShare() {
        if (!sharing) return;
        const groups = Array.from($('recordings-share-groups').querySelectorAll('input:checked'))
            .map((box) => box.value);
        const users = $('recordings-share-users').value.split(/[\s,;]+/).filter(Boolean);
        const response = await fetch(`/api/recordings/${encodeURIComponent(sharing.id)}/shares`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ groups, users }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            $('recordings-share-error').textContent =
                data.error || t('common.hub_error', { status: response.status });
            $('recordings-share-error').hidden = false;
            return;
        }
        shareDialog.close();
        sharing = null;
        void load();
    }

    $('recordings-share-save').addEventListener('click', saveShare);
    $('recordings-share-cancel').addEventListener('click', () => shareDialog.close());

    // ---- deleting --------------------------------------------------------------------
    // The console's shared confirmDialog (common.js): destructive, so the focus starts on
    // Cancel and a reflexive Enter deletes nothing.
    async function openDelete(rec) {
        const ok = await confirmDialog({
            title: t('recordings.delete_title'),
            message: t('recordings.delete_confirm',
                       { machine: rec.machine, time: fmtTime(rec.created_at) }),
            confirmLabel: t('common.delete'),
            danger: true,
        });
        if (!ok) return;
        const response = await fetch(`/api/recordings/${encodeURIComponent(rec.id)}`,
                                     { method: 'DELETE' });
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            showError(data.error || t('common.hub_error', { status: response.status }));
        }
        void load();
    }

    void load();
})();
