// Shared helpers used across dashboard/machine/history pages.

// ---- CSRF token interceptor ----
// Reads the token from <meta name="csrf-token"> and attaches it as the
// X-CSRF-Token header on every state-changing fetch.  The server issues the
// token in the session cookie and exposes it via the base.html meta tag.
// login_required validates the header on POST/PUT/DELETE/PATCH for
// cookie-authenticated requests.
(function () {
    const _origFetch = window.fetch;
    window.fetch = function (input, init) {
        init = init || {};
        const method = (init.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
            const meta = document.querySelector('meta[name="csrf-token"]');
            const token = meta && meta.getAttribute('content');
            if (token) {
                const headers = new Headers(init.headers || {});
                if (!headers.has('X-CSRF-Token')) {
                    headers.set('X-CSRF-Token', token);
                }
                init.headers = headers;
            }
        }
        return _origFetch.call(this, input, init);
    };
})();

const THEME_STORAGE_KEY = 'tempmonitor:theme';
const SIDEBAR_STORAGE_KEY = 'tempmonitor:sidebar';

function formatUptime(seconds) {
    // null is "no uptime reported", not zero: Number(null) is 0, which used to print "0m"
    // for every machine whose agent never sent one.
    if (seconds === null || seconds === undefined || seconds === '') return '--';
    const value = Number(seconds);
    if (!Number.isFinite(value)) return '--';
    const total = Math.max(0, Math.floor(value));
    // A machine that booted seconds ago is up, not up for "0m" -- which reads as stopped.
    if (total < 60) return '<1m';
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
// Uses DOM manipulation (createElement + textContent) instead of innerHTML to
// prevent XSS -- even though current callers only pass catalog keys, any future
// caller passing user-controlled text would otherwise create a vulnerability.
function setStatusPill(el, state, label) {
    if (!el) return;
    el.classList.remove('status-pill--ok', 'status-pill--warn', 'status-pill--danger', 'status-pill--muted');
    el.classList.add(`status-pill--${state}`);
    el.textContent = '';
    const dot = document.createElement('span');
    dot.className = 'status-pill__dot';
    el.appendChild(dot);
    el.appendChild(document.createTextNode(label));
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

// Rewrite this page's URL in place, and the shell's address bar with it.
//
// replaceState, never pushState: shell.js's popstate handler re-navigates the frame
// whenever the frame's href and location.href disagree, so pushing entries from inside a
// framed page makes Back walk in-page state changes AND fight the shell's own history.
// shell.js's go() uses location.replace for exactly this reason.
//
// The second half is the part that is easy to miss. A replaceState inside the frame does
// not move the shell's address bar -- the shell only syncs on frame load -- so a tab or
// machine chosen after load would vanish on refresh and be absent from any bookmark or
// copied link. Same-origin by construction, guarded like shellElement() above.
function syncUrl(url) {
    try { history.replaceState(history.state, '', url); } catch (e) { /* opaque origin */ }
    try {
        if (window.parent !== window && window.parent.history) {
            window.parent.history.replaceState(window.parent.history.state, '', url);
        }
    } catch (e) { /* not our shell */ }
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

// Desktop sidebar collapse: 240px of labelled nav <-> a 64px icon rail, so the wide pages
// get the difference. Distinct from initMobileNav() above, which is the drawer: that one is
// about a viewport with no room for a sidebar at all, this one is about choosing to spend
// the room on the page instead. Below the breakpoint the button is hidden (components.css)
// and this state is inert -- the drawer ignores it.
//
// The attribute lives on <html>, not on .app-shell, because base.html's inline head script
// has to set it before first paint and .app-shell does not exist yet at that point. Under
// the app shell only the shell document has a sidebar, so there is nothing to keep in sync
// across the frame boundary.
function initSidebarCollapse() {
    const toggle = document.getElementById('nav-collapse');
    if (!toggle) return;
    const root = document.documentElement;
    const sidebar = document.getElementById('app-sidebar');

    const isCollapsed = () => root.getAttribute('data-sidebar') === 'collapsed';

    function sync() {
        const collapsed = isCollapsed();
        toggle.setAttribute('aria-expanded', String(!collapsed));
        const label = t(collapsed ? 'nav.expand' : 'nav.collapse');
        toggle.setAttribute('aria-label', label);
        toggle.title = label;
        // The rail has no room for the link text, so the text becomes the tooltip -- read
        // off the label element rather than kept in a second list, which would drift the
        // first time a nav entry is renamed. Removed again when expanded: a tooltip that
        // repeats the word already on screen is just noise following the pointer.
        if (sidebar) {
            for (const link of sidebar.querySelectorAll('.sidebar__link')) {
                const text = (link.querySelector('.sidebar__link-label')?.textContent || '').trim();
                if (collapsed && text) link.title = text;
                else link.removeAttribute('title');
            }
        }
        // Same trick for the update notice, which shrinks to a bare "!" on the rail.
        const notice = document.getElementById('hub-update-notice');
        const noticeText = document.getElementById('hub-update-text');
        if (notice && noticeText) {
            if (collapsed) notice.title = (noticeText.textContent || '').trim();
            else notice.removeAttribute('title');
        }
    }

    function setCollapsed(collapsed) {
        if (collapsed) root.setAttribute('data-sidebar', 'collapsed');
        else root.removeAttribute('data-sidebar');
        try { localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? 'collapsed' : 'expanded'); }
        catch (e) { /* private mode: the choice just does not survive the reload */ }
        sync();
    }

    toggle.addEventListener('click', () => setCollapsed(!isCollapsed()));

    // On the rail the notice is a warning square with nothing to click inside it, so the
    // click means "show me what this is about". Capture, because the real notice carries
    // its own buttons and we must not let a hidden Update-now be activated blind.
    const notice = document.getElementById('hub-update-notice');
    if (notice) {
        notice.addEventListener('click', (e) => {
            if (!isCollapsed()) return;
            e.preventDefault();
            e.stopPropagation();
            setCollapsed(false);
        }, true);
    }

    // The poller rewrites the notice's sentence as versions change; re-sync so the rail's
    // tooltip does not keep quoting a version that has already been applied.
    document.addEventListener('hubupdate:change', sync);

    sync();
}

// The Administration fold (see the comment above it in _sidebar.html). Remembered per browser
// like the rail, but never allowed to hide where you ARE: a folded section holding the active
// link opens itself, without saving that, so the operator's own choice survives the visit.
// shell.js calls window.FleetNavFold.reveal() after every frame navigation for the same
// reason -- the sidebar outlives the pages, so "active" changes without a reload.
const NAV_FOLD_STORAGE_PREFIX = 'tempmonitor:nav-fold:';

function initNavFold() {
    const sections = document.querySelectorAll('.sidebar__section[data-foldable]');
    if (!sections.length) return;

    function setFolded(section, folded) {
        const toggle = section.querySelector('.sidebar__section-toggle');
        section.toggleAttribute('data-folded', folded);
        if (toggle) toggle.setAttribute('aria-expanded', String(!folded));
    }

    function reveal() {
        for (const section of sections) {
            if (section.querySelector('.sidebar__link--active')) setFolded(section, false);
        }
    }

    for (const section of sections) {
        const toggle = section.querySelector('.sidebar__section-toggle');
        if (!toggle) continue;
        const key = NAV_FOLD_STORAGE_PREFIX + (section.getAttribute('aria-labelledby') || 'section');
        let saved = null;
        try { saved = localStorage.getItem(key); } catch (e) { /* private mode */ }
        setFolded(section, saved === 'folded');
        toggle.addEventListener('click', () => {
            const folded = !section.hasAttribute('data-folded');
            setFolded(section, folded);
            try { localStorage.setItem(key, folded ? 'folded' : 'open'); }
            catch (e) { /* the choice just does not survive the reload */ }
        });
    }
    reveal();
    window.FleetNavFold = { reveal };
}

// "Skip to content". On a classic page the #main-content fragment does the work by itself;
// in the shell the content is a framed document, so follow the link into whichever frame is
// on screen and focus ITS <main> -- focusing the <iframe> alone leaves the next Tab back in
// the sidebar in some browsers.
function initSkipLink() {
    const link = document.querySelector('.skip-link');
    if (!link) return;
    link.addEventListener('click', (e) => {
        const frame = document.querySelector('.app-frames__frame:not([hidden])');
        if (!frame) return;              // classic page: the fragment is enough
        e.preventDefault();
        let target = null;
        try {
            target = frame.contentDocument && frame.contentDocument.getElementById('main-content');
        } catch (err) { /* a document we may not touch: focusing the frame is the best left */ }
        frame.focus();
        if (target) target.focus();
    });
}

// Page intros: the paragraph under every page's <h1>. Many are design rationale several lines
// long ("Alerts" ran to four), set above the content, so on a phone the Devices list began a
// quarter of the way down the screen. Clamped to two lines with a More/Less toggle, found by
// SHAPE -- the h1 and the .stat-card__meta paragraph straight after it, the pattern every page
// template uses -- so no page had to change. The toggle only appears when the text is
// actually cut off; a short intro is left alone.
function initPageIntro() {
    const main = document.getElementById('main-content');
    const heading = main && main.querySelector('h1');
    const intro = heading && heading.nextElementSibling;
    if (!intro || intro.tagName !== 'P' || !intro.classList.contains('stat-card__meta')) return;

    intro.classList.add('page-intro');
    if (!intro.id) intro.id = 'page-intro';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'page-intro__toggle';
    toggle.setAttribute('aria-controls', intro.id);
    toggle.hidden = true;
    intro.after(toggle);

    function render() {
        const open = intro.classList.contains('page-intro--open');
        toggle.textContent = t(open ? 'common.intro_less' : 'common.intro_more');
        toggle.setAttribute('aria-expanded', String(open));
    }

    // Whether the clamp is hiding anything. Re-asked on resize, since a two-line intro on a
    // desktop is six lines on a phone; an expanded one keeps its toggle so it can close.
    // The intro's own bottom margin (inline on most pages, absent on some) moves to the
    // toggle while it shows, so the pair keeps whatever gap the page gave the paragraph.
    // Restored to the page's own inline value, not to '' -- most intros set their margin in a
    // style attribute, and clearing the property would delete it.
    const introMargin = getComputedStyle(intro).marginBottom;
    const inlineMargin = intro.style.marginBottom;
    function measure() {
        if (intro.classList.contains('page-intro--open')) return;
        toggle.hidden = intro.scrollHeight <= intro.clientHeight + 1;
        intro.style.marginBottom = toggle.hidden ? inlineMargin : '0';
        toggle.style.marginBottom = toggle.hidden ? '' : introMargin;
    }

    toggle.addEventListener('click', () => {
        intro.classList.toggle('page-intro--open');
        render();
        measure();
    });
    render();
    measure();
    window.addEventListener('resize', measure);
}

// The account menu (every width): email, language, version badges and Sign out.
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

    // Wrapper, so the announcement happens once however `paint` returned -- it has four
    // exits. The collapsed sidebar listens: on the rail the notice is a bare "!" and this
    // sentence is its tooltip, which would otherwise keep quoting a version that has
    // already been installed.
    function render(data) {
        paint(data);
        document.dispatchEvent(new CustomEvent('hubupdate:change'));
    }

    function paint(data) {
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

        // A container hub is updated by pulling its image, never from here: the POST would
        // only be refused, so there is no button to press.
        const imageMode = data.update_mode === 'image';
        els.action.hidden = imageMode;
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
        els.text.textContent = imageMode
            ? t('hub_update.available_image', { version: latest })
            : t('hub_update.available', { version: latest });
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
    initSidebarCollapse();
    initNavFold();
    initSkipLink();
    initPageIntro();
    initTopbarMore();
    initAlertBadge();
    initHubUpdate();
});

// ============ Toasts ============
// A short message that does not need its own place on the page: "asked for PC-7 to be
// woken", "could not delete X". Added in hub 1.114.0 for the Devices page, whose row menu and
// bulk bar act on PCs that may have scrolled out of view by the time the answer arrives --
// there is no status line near the thing that was clicked.
//
// Deliberately NOT for results an operator must not miss. The Processes card keeps its own
// line above the table (see machine.html) because a message that disappears is a message that
// gets missed; the same reasoning is why an ERROR toast here stays until it is dismissed.
//
// Rendered into the document that calls it, framed page or not: the page that did the work is
// the one on screen, and reaching into the shell for a container would be a second owner of
// a page's feedback. Built with textContent -- messages routinely quote machine names.
function toast(message, { kind = 'info', timeout = 6000 } = {}) {
    let host = document.getElementById('toast-host');
    if (!host) {
        host = document.createElement('div');
        host.id = 'toast-host';
        host.setAttribute('role', 'status');
        host.setAttribute('aria-live', 'polite');
        host.className = 'pointer-events-none fixed right-4 bottom-4 z-[70] flex max-w-[calc(100vw-2rem)] flex-col items-end gap-2';
        document.body.appendChild(host);
    }
    const item = document.createElement('div');
    item.className = 'pointer-events-auto flex w-80 max-w-full items-start gap-3 rounded-lg border border-card-border bg-card px-4 py-3 text-sm text-text shadow-lg';
    const dot = document.createElement('span');
    dot.className = 'mt-1.5 size-2 shrink-0 rounded-full '
        + (kind === 'error' ? 'bg-danger' : kind === 'success' ? 'bg-success' : 'bg-accent');
    const text = document.createElement('span');
    text.className = 'min-w-0 flex-1 break-words';
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'shrink-0 cursor-pointer border-0 bg-transparent p-0 text-base leading-none text-muted hover:text-text';
    close.setAttribute('aria-label', t('toast.dismiss'));
    close.textContent = '×';
    close.addEventListener('click', () => item.remove());
    item.append(dot, text, close);
    host.appendChild(item);
    if (kind !== 'error' && timeout) setTimeout(() => item.remove(), timeout);
    return item;
}

// ============ Dialogs ============
// The console's own replacement for the browser's confirm() and alert(). Those boxes were
// on every destructive button in the console, and they are the one thing on a page that
// cannot be styled, translated in their chrome, or titled: the operator was asked "Delete
// the policy X?" under a header reading "localhost:5000 says", with an OK button that said
// nothing about what OK would do. Here the button names the act ("Delete", "Revoke",
// "Install now"), and a destructive one is drawn as such.
//
// Built on <dialog>.showModal() like every other modal here (see .modal in components.css)
// and built fresh per call, then removed: one shared element would need a queue for the
// second question asked while the first is still open, and a fresh one simply stacks.
//
// Decided on the buttons' click and the dialog's `cancel` (Escape), NOT on `close`: `close`
// is queued as a later task, and assistant.js's first delete dialog, which waited for it,
// never deleted anything in a frame the browser was not drawing.
//
// Where a toast would do, use toast(). It renders under document.body, and a modal <dialog>
// sits in the top layer above every z-index -- so a toast raised while a dialog is open is
// painted BEHIND it. An error raised from inside an open dialog is a noticeDialog() instead.
// Only has to be unique within this page, which a counter is -- and a counter is not the
// pseudorandom value a reader then has to check is not guarding anything.
let _dialogSeq = 0;

function _openDialog({ title, message, buttons, initialFocus }) {
    return new Promise((resolve) => {
        const dialog = document.createElement('dialog');
        dialog.className = 'modal modal--compact';
        const titleId = `fh-dialog-${++_dialogSeq}`;
        dialog.setAttribute('aria-labelledby', titleId);

        const head = document.createElement('div');
        head.className = 'modal__head';
        const h2 = document.createElement('h2');
        h2.className = 'modal__title';
        h2.id = titleId;
        h2.textContent = title;
        head.appendChild(h2);

        // textContent, never markup: these messages quote machine, group and file names.
        // .modal__message keeps the "\n\n" the catalog uses to split a question from its
        // consequences, which a native confirm() honoured and a <p> would collapse.
        const body = document.createElement('div');
        body.className = 'modal__body';
        const text = document.createElement('p');
        text.className = 'modal__message';
        text.textContent = message || '';
        body.appendChild(text);

        const foot = document.createElement('div');
        foot.className = 'modal__foot';

        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            dialog.removeEventListener('cancel', onCancel);
            if (dialog.open) dialog.close();
            dialog.remove();
            resolve(value);
        };
        const onCancel = () => finish(buttons.find((b) => b.cancel)?.value);
        dialog.addEventListener('cancel', onCancel);

        const nodes = buttons.map((spec) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = spec.className;
            button.textContent = spec.label;
            button.addEventListener('click', () => finish(spec.value));
            foot.appendChild(button);
            return button;
        });

        dialog.append(head, body, foot);
        document.body.appendChild(dialog);
        dialog.showModal();
        (nodes[initialFocus] || nodes[nodes.length - 1]).focus();
    });
}

/** Ask before doing something. Resolves true only when the confirm button was pressed;
 *  Cancel and Escape both resolve false.
 *
 *  `danger` is for anything that removes, revokes or cannot be undone. It draws the
 *  button as destructive AND puts the focus on Cancel, so a reflexive Enter does nothing --
 *  the one property of the native confirm() worth losing, since its focus sat on OK. */
function confirmDialog({ title, message, confirmLabel, cancelLabel, danger = false } = {}) {
    return _openDialog({
        title: title || t('dialog.confirm_title'),
        message,
        buttons: [
            { label: cancelLabel || t('common.cancel'), className: 'btn btn--ghost',
              value: false, cancel: true },
            { label: confirmLabel || t('dialog.ok'),
              className: danger ? 'btn btn--danger' : 'btn btn--primary', value: true },
        ],
        initialFocus: danger ? 0 : 1,
    });
}

/** Tell the operator something they must read before carrying on -- the outcome of an
 *  action that only partly happened, or an error raised while another dialog is open.
 *  Anything less than that is a toast(). Resolves once it is closed. */
function noticeDialog({ title, message, kind = 'info', closeLabel } = {}) {
    return _openDialog({
        title: title || t(kind === 'error' ? 'dialog.error_title' : 'dialog.notice_title'),
        message,
        buttons: [
            { label: closeLabel || t('dialog.ok'), className: 'btn btn--primary',
              value: undefined, cancel: true },
        ],
        initialFocus: 0,
    }).then(() => undefined);
}

// ---- Tiny per-element builders shared by the pages ----
//
// These two were once copied into a dozen page scripts. They live here now; the per-page
// copies were byte-identical and are deleted. Pages that need a different shape (the
// settings pages' children-props variant, dashboard/patches' String() coercion) still
// define their own in their IIFE scope, which shadows the global harmlessly.

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function fmtTime(epoch) {
    if (!epoch) return '—';
    return new Date(epoch * 1000).toLocaleString();
}
