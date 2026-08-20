// Shared helpers used across dashboard/machine/history pages.

const THEME_STORAGE_KEY = 'tempmonitor:theme';

function formatUptime(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value)) return '--';
    const total = Math.max(0, Math.floor(value));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const parts = [];
    if (days) parts.push(`${days}d`);
    if (days || hours) parts.push(`${hours}h`);
    parts.push(`${minutes}m`);
    return parts.join(' ');
}

// Writes state-dot + label + color-modifier onto a .status-pill element.
function setStatusPill(el, state, label) {
    if (!el) return;
    el.classList.remove('status-pill--ok', 'status-pill--warn', 'status-pill--danger', 'status-pill--muted');
    el.classList.add(`status-pill--${state}`);
    el.innerHTML = `<span class="status-pill__dot"></span>${label}`;
}

// A machine's online/offline pill, qualified when it has no fleet enrollment.
//
// **Why "not enrolled" is worth saying out loud.** An agent that never enrolled still posts
// telemetry -- /api/report is open by design -- so the machine appears with a name, a model
// and a live temperature and reads as entirely healthy. What it has no channel for is
// everything the console is FOR: commands, the terminal, package deployments, backups and
// the process list all queue or wait and quietly never happen. That is invisible until
// somebody tries one, which is why it belongs on the same pill as online/offline rather than
// behind a click.
//
// **`enrolled === false`, not falsy.** The field is absent on an older hub's response and
// briefly unknown for a card the live socket created before the next /api/machines poll;
// neither is evidence of anything, and claiming "not enrolled" on a missing field would
// label a healthy fleet.
//
// Colour follows what an operator can DO about it: an unenrolled machine that is up right
// now is fixable (re-run the installer with the enrollment secret), so it warns; one that is
// off is a note for when it comes back, so it stays muted like any other offline row.
function setMachineStatusPill(el, row) {
    if (!el) return;
    const online = row && row.status === 'online';
    const unenrolled = row && row.enrolled === false;
    // Literal keys, never an interpolated one: setStatusPill writes the label with innerHTML,
    // and tests/test_i18n.py's key scan can only see literals.
    const label = online
        ? (unenrolled ? t('common.status.online_unenrolled') : t('common.status.online'))
        : (unenrolled ? t('common.status.offline_unenrolled') : t('common.status.offline'));
    setStatusPill(el, online ? (unenrolled ? 'warn' : 'ok') : 'muted', label);
    // Set as a property, so an explanation this long does not have to be markup-safe.
    el.title = unenrolled ? t('common.status.unenrolled_help') : '';
}

// An element of the app chrome, wherever this page happens to be rendered. Under the app
// shell (see shell.js) the topbar belongs to the parent document, not to this one, so a page
// that owns a piece of chrome has to reach out of its frame for it. Same-origin, so this is
// an ordinary lookup; the guard is for the day something else frames us.
function shellElement(id) {
    const local = document.getElementById(id);
    if (local) return local;
    try {
        if (window.parent !== window && window.parent.document) {
            return window.parent.document.getElementById(id);
        }
    } catch (e) { /* cross-origin parent: not our shell, and none of our business */ }
    return null;
}

// Connects the Socket.IO client and wires up a #socket-status pill. Returns the socket
// so callers can attach their own `new_temp` handlers.
function connectSocketWithStatus() {
    const socket = io({ transports: ['polling'], upgrade: false });
    const statusEl = shellElement('socket-status');
    // The shell renders one pill for every page and hides it until a page claims it. Claiming
    // is marking our own frame, not showing the pill directly: the shell keeps several frames
    // alive at once and only the visible one's status should be on screen.
    if (window.frameElement) window.frameElement.dataset.socket = 'yes';
    if (statusEl) statusEl.hidden = false;
    socket.on('connect', () => setStatusPill(statusEl, 'ok', t('common.status.live')));
    socket.on('disconnect', () => setStatusPill(statusEl, 'danger', t('common.status.offline')));
    return socket;
}

function initThemeToggle() {
    const toggle = document.getElementById('theme-toggle');
    if (!toggle) return;
    const root = document.documentElement;

    const sync = () => toggle.setAttribute('aria-pressed', String(root.getAttribute('data-theme') === 'light'));
    sync();

    toggle.addEventListener('click', () => {
        const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        root.setAttribute('data-theme', next);
        try { localStorage.setItem(THEME_STORAGE_KEY, next); } catch (e) { /* ignore */ }
        sync();
        // Under the app shell the toggle is in the chrome and the pages are in frames, which
        // this attribute does not reach. Announced rather than reached into so this stays the
        // theme's own business and shell.js keeps the frames its.
        document.dispatchEvent(new CustomEvent('theme:change', { detail: { theme: next } }));
    });
}

// Below the CSS breakpoint the sidebar is an off-canvas drawer (components.css) and this
// owns its open/closed state. Same element, same links -- there is no separate mobile nav.
function initMobileNav() {
    const toggle = document.getElementById('nav-toggle');
    const sidebar = document.getElementById('app-sidebar');
    const scrim = document.getElementById('nav-scrim');
    if (!toggle || !sidebar || !scrim) return;

    const closeBtn = document.getElementById('nav-close');
    const isOpen = () => sidebar.classList.contains('sidebar--open');

    function setOpen(open) {
        sidebar.classList.toggle('sidebar--open', open);
        scrim.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        // The drawer scrolls itself; letting the page scroll underneath it is disorienting.
        document.body.style.overflow = open ? 'hidden' : '';
        if (open) {
            const first = sidebar.querySelector('.sidebar__link');
            if (first) first.focus();
        }
    }

    function close({ restoreFocus = false } = {}) {
        if (!isOpen()) return;
        setOpen(false);
        if (restoreFocus) toggle.focus();
    }

    toggle.addEventListener('click', () => setOpen(!isOpen()));
    scrim.addEventListener('click', () => close());
    if (closeBtn) closeBtn.addEventListener('click', () => close({ restoreFocus: true }));

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') close({ restoreFocus: true });
    });

    // Under the app shell this is the ONLY thing that closes the drawer: the sidebar outlives
    // the page it navigates to, so there is no longer a page load to take it away with.
    sidebar.addEventListener('click', (e) => {
        if (e.target.closest('.sidebar__link')) close();
    });

    // Rotating or resizing past the breakpoint puts the sidebar back in the layout; an
    // --open class left behind would strand the scrim and the body scroll lock.
    const narrow = window.matchMedia('(max-width: 900px)');
    const onChange = () => { if (!narrow.matches) close(); };
    if (narrow.addEventListener) narrow.addEventListener('change', onChange);
    else narrow.addListener(onChange);  // Safari < 14
}

// Mobile overflow menu holding the version badges, the signed-in email and Sign out.
function initTopbarMore() {
    const toggle = document.getElementById('topbar-more');
    const menu = document.getElementById('topbar-meta');
    if (!toggle || !menu) return;

    const isOpen = () => menu.classList.contains('topbar__meta--open');

    function setOpen(open) {
        menu.classList.toggle('topbar__meta--open', open);
        toggle.setAttribute('aria-expanded', String(open));
    }

    toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        setOpen(!isOpen());
    });

    document.addEventListener('click', (e) => {
        if (isOpen() && !menu.contains(e.target)) setOpen(false);
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && isOpen()) {
            setOpen(false);
            toggle.focus();
        }
    });
}

// ============ Open-alert badge ============
// The sidebar badge is rendered server-side once per page load, which goes stale the moment
// an alert is dismissed -- in another tab, by another operator, or on the Alerts page of a
// shell that never reloads this document. So every page that shows the badge keeps it
// current itself.

const ALERT_BADGE_POLL_MS = 30000;

// In shell mode the sidebar lives in the OUTER document while pages run inside the frame,
// so a framed page finds its badge through the parent. Same origin by construction; the
// try/catch is for the case where it isn't ours to touch.
function alertBadgeEl() {
    const own = document.getElementById('alerts-badge');
    if (own) return own;
    try {
        if (window.parent && window.parent !== window) {
            return window.parent.document.getElementById('alerts-badge');
        }
    } catch (e) { /* cross-origin parent: not our chrome */ }
    return null;
}

// Zero hides the badge rather than showing a "0" -- nothing to attend to should look like
// nothing, which is what the server-rendered markup does too.
function setAlertBadge(count) {
    const el = alertBadgeEl();
    if (!el) return;
    el.textContent = count ? String(count) : '';
    el.hidden = !count;
}

async function refreshAlertBadge() {
    try {
        const resp = await fetch('/api/alerts/count');
        if (!resp.ok) return;
        const data = await resp.json();
        if (typeof data.count === 'number') setAlertBadge(data.count);
    } catch (e) { /* offline or mid-deploy: keep the last number, don't blank it */ }
}

function initAlertBadge() {
    // Only the document that OWNS the badge polls. A framed page shares the shell's badge
    // and would otherwise double the request rate for one number; it can still push a value
    // through setAlertBadge (alerts.js does, the instant a dismiss is confirmed).
    if (!document.getElementById('alerts-badge')) return;
    setInterval(refreshAlertBadge, ALERT_BADGE_POLL_MS);
    // A background tab's timers are throttled hard, so a tab returned to after an hour would
    // show its hour-old count for a while. Correct it on the way back in.
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshAlertBadge();
    });
}

// ============ Hub update notice ============
// Bottom-of-sidebar notice for "main has a newer hub than this one, and this hub is not
// going to install it by itself". The server does the comparing (hub_update_watcher polls
// GitHub every 15 minutes and caches the answer); /api/hub/version just reports it, so
// polling this faster than the watcher refreshes it would buy nothing.
//
// Everything here is gated on the element existing, and the element only renders for
// operators with manage_settings -- so on every other account this whole section is a
// single null check and stops.

const HUB_UPDATE_POLL_MS = 5 * 60 * 1000;
// While an update we started is applying, the hub disappears for a few seconds and comes
// back on the new version. Poll fast through that window so the notice clears promptly.
const HUB_UPDATE_RESTART_POLL_MS = 3000;
const HUB_UPDATE_DISMISS_KEY = 'tempmonitor:hubUpdateDismissed';

// Same reach-through as alertBadgeEl(): in shell mode the sidebar lives in the OUTER
// document. Kept separate rather than generalised because only the owning document ever
// calls this -- initHubUpdate() returns early in the frame.
function hubUpdateEls() {
    const notice = document.getElementById('hub-update-notice');
    if (!notice) return null;
    return {
        notice,
        text: document.getElementById('hub-update-text'),
        action: document.getElementById('hub-update-action'),
        dismiss: document.getElementById('hub-update-dismiss'),
    };
}

function initHubUpdate() {
    const els = hubUpdateEls();
    // Not our document (framed page), or an operator without manage_settings.
    if (!els) return;

    let timer = null;
    let requestedByUs = false;

    function schedule(ms) {
        if (timer) clearTimeout(timer);
        timer = setTimeout(refresh, ms);
    }

    function dismissedVersion() {
        try {
            return localStorage.getItem(HUB_UPDATE_DISMISS_KEY);
        } catch (e) {
            return null;  // storage disabled: the notice simply is not dismissible
        }
    }

    function render(data) {
        const latest = data.latest || '';
        els.notice.dataset.hubLatest = latest;

        if (data.status === 'running') {
            els.text.textContent = t('hub_update.updating');
            els.action.hidden = true;
            // No dismissing something that is already rewriting the hub underneath us.
            els.dismiss.hidden = true;
            els.notice.hidden = false;
            return;
        }

        // The update we asked for finished: the hub is back on a version with nothing
        // newer to fetch. Reload so the topbar version badge and the rest of the page
        // stop describing the build that is no longer running.
        if (requestedByUs && !data.update_available) {
            window.location.reload();
            return;
        }

        els.action.hidden = false;
        els.dismiss.hidden = false;

        if (data.status === 'failed') {
            // Left visible with the action button intact, so a failure that was a
            // transient network problem can simply be retried.
            els.text.textContent = data.error
                ? `${t('hub_update.failed')} (${data.error})`
                : t('hub_update.failed');
            els.notice.hidden = false;
            return;
        }

        // auto_update on means the watcher will install this without anyone's help --
        // announcing it would be noise, and the "Update now" button a race.
        const relevant = data.update_available && !data.auto_update;
        els.text.textContent = t('hub_update.available', { version: latest });
        els.notice.hidden = !relevant || dismissedVersion() === latest;
    }

    async function refresh() {
        let running = false;
        try {
            const resp = await fetch('/api/hub/version');
            if (resp.ok) {
                const data = await resp.json();
                running = data.status === 'running';
                render(data);
            }
        } catch (e) {
            // Offline, or the hub mid-restart because we just told it to update. Keep
            // whatever the notice currently says and try again -- blanking it here would
            // erase the "updating" message at exactly the moment it is true.
            running = requestedByUs;
        }
        schedule(running || requestedByUs ? HUB_UPDATE_RESTART_POLL_MS : HUB_UPDATE_POLL_MS);
    }

    els.action.addEventListener('click', async () => {
        els.action.disabled = true;
        try {
            const resp = await fetch('/api/hub/update', {
                method: 'POST',
                // Not decoration: the endpoint reads the body as JSON, and requiring this
                // content type is what makes a cross-origin form unable to reach it.
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                render({ status: 'failed', error: data.error || `http ${resp.status}`,
                         latest: els.notice.dataset.hubLatest, update_available: true });
                return;
            }
            requestedByUs = true;
            render({ status: 'running', latest: els.notice.dataset.hubLatest });
            schedule(HUB_UPDATE_RESTART_POLL_MS);
        } finally {
            els.action.disabled = false;
        }
    });

    els.dismiss.addEventListener('click', () => {
        try {
            // Stamped with the version, so the notice comes back for the NEXT release
            // rather than being silenced forever by one click.
            localStorage.setItem(HUB_UPDATE_DISMISS_KEY, els.notice.dataset.hubLatest || '');
        } catch (e) { /* storage disabled: hide it for this page view only */ }
        els.notice.hidden = true;
    });

    // The server-rendered state is already correct for this page load; the first poll is
    // for the tab left open across a release.
    if (dismissedVersion() && dismissedVersion() === els.notice.dataset.hubLatest) {
        els.notice.hidden = true;
    }
    schedule(HUB_UPDATE_POLL_MS);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refresh();
    });
}

document.addEventListener('DOMContentLoaded', () => {
    initThemeToggle();
    initMobileNav();
    initTopbarMore();
    initAlertBadge();
    initHubUpdate();
});
