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

function requestNotificationPermission() {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission !== 'granted' && Notification.permission !== 'denied') {
        Notification.requestPermission();
    }
}

// Distinguishes "hot because it's under heavy load" (expected) from "hot while
// mostly idle" (worth investigating -- possible cooling/thermal-paste/dust issue).
// Unknown load (older agent, no sensors yet) conservatively reads as "investigate".
function classifyTemperatureStatus(temp, highTempThreshold, cpuLoadPct, lowLoadThreshold) {
    if (temp === undefined || temp === null || temp < highTempThreshold) return 'normal';
    if (typeof cpuLoadPct === 'number' && cpuLoadPct >= lowLoadThreshold) return 'high-temp-expected';
    return 'high-temp-investigate';
}

function notifyHighTemp(machine, temp) {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    new Notification(t('common.high_temp_title'), {
        body: t('common.high_temp_body', { machine: machine, temp: temp }),
        icon: 'https://cdn-icons-png.flaticon.com/512/3248/3248139.png'
    });
}

// Connects the Socket.IO client and wires up a #socket-status pill. Returns the socket
// so callers can attach their own `new_temp` handlers.
function connectSocketWithStatus() {
    const socket = io({ transports: ['polling'], upgrade: false });
    const statusEl = document.getElementById('socket-status');
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

    // Navigating is a full page load, so this only matters for a link that no-ops --
    // but a drawer that stays open after a tap reads as broken.
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

document.addEventListener('DOMContentLoaded', () => {
    initThemeToggle();
    initMobileNav();
    initTopbarMore();
});
