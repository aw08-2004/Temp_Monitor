// Download Client page (roadmap #11).
//
// Renders exactly what the SIGNED client manifest declares -- one card per build -- and
// renders nothing at all when the hub could not verify it. The verification happens
// server-side in clientrelease.py; this file's job is to show the answer honestly,
// including when the answer is "no client has been published yet", which is a different
// state from "the signature is wrong" and is shown as such.
//
// The sha256 is displayed beside every file, because it is the same digest the client
// itself checks on self-update: an operator who wants to know they got the right bytes
// should not have to take the page's word for it.

const host = document.getElementById('download-host');
const versionBadge = document.getElementById('download-version');

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function formatSize(bytes) {
    if (!bytes) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function platformLabel(build) {
    // The manifest may name a platform this console has never heard of -- a build should
    // still be offered rather than dropped, so an unknown slug falls back to itself.
    if (build.label) return build.label;
    const key = `download.platform.${build.platform}`;
    const translated = t(key);
    const base = translated === key ? (build.platform || t('download.platform.unknown'))
                                    : translated;
    return build.arch ? `${base} (${build.arch})` : base;
}

function buildCard(build) {
    const card = el('div', 'card');
    card.appendChild(el('h2', 'card__title', platformLabel(build)));

    if (build.notes) card.appendChild(el('p', 'stat-card__meta', build.notes));

    const meta = [];
    if (build.filename) meta.push(build.filename);
    if (build.size) meta.push(formatSize(build.size));
    if (meta.length) {
        card.appendChild(el('p', 'stat-card__meta', meta.join(' · ')));
    }

    const link = el('a', 'btn btn--primary',
        build.kind === 'link' ? t('download.open') : t('download.get'));
    link.href = build.url;
    if (build.kind === 'link') {
        // Somewhere else entirely (TestFlight, an MDM portal). Opening it in this tab
        // would take an operator out of the console mid-task.
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
    } else {
        link.setAttribute('download', build.filename || '');
    }
    const actions = el('div', 'card-actions');
    actions.style.marginTop = 'var(--space-4)';
    actions.appendChild(link);
    card.appendChild(actions);

    if (build.sha256) {
        const digest = el('p', 'stat-card__meta', build.sha256);
        digest.style.fontFamily = 'var(--font-mono, monospace)';
        digest.style.wordBreak = 'break-all';
        digest.style.marginTop = 'var(--space-3)';
        const label = el('p', 'stat-card__meta', t('download.checksum'));
        label.style.marginTop = 'var(--space-3)';
        card.appendChild(label);
        card.appendChild(digest);
    }
    return card;
}

function renderError(message) {
    host.replaceChildren();
    const empty = el('div', 'empty-state');
    empty.appendChild(el('p', null, t('download.unavailable')));
    // The server's own sentence, not a generic one: "no release yet" and "the signature
    // does not verify" need completely different actions from whoever is reading.
    empty.appendChild(el('p', 'stat-card__meta', message));
    host.appendChild(empty);
}

async function load() {
    let resp;
    try {
        resp = await fetch('/api/app/manifest');
    } catch (err) {
        renderError(err.message);
        return;
    }

    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok || !body) {
        renderError((body && body.error) || `HTTP ${resp.status}`);
        return;
    }

    versionBadge.textContent = t('download.version', { version: body.version });
    versionBadge.hidden = false;

    host.replaceChildren();
    const grid = el('div', 'card-grid');
    body.builds.forEach((build) => grid.appendChild(buildCard(build)));
    host.appendChild(grid);

    if (body.notes) {
        const notes = el('p', 'stat-card__meta', body.notes);
        notes.style.marginTop = 'var(--space-5)';
        host.appendChild(notes);
    }
}

load();
