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

function notifyOverheat(machine, temp) {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    new Notification('CPU Overheat Alert!', {
        body: `${machine} is overheating at ${temp}°C!`,
        icon: 'https://cdn-icons-png.flaticon.com/512/3248/3248139.png'
    });
}

// Connects the Socket.IO client and wires up a #socket-status pill. Returns the socket
// so callers can attach their own `new_temp` handlers.
function connectSocketWithStatus() {
    const socket = io({ transports: ['polling'], upgrade: false });
    const statusEl = document.getElementById('socket-status');
    socket.on('connect', () => setStatusPill(statusEl, 'ok', 'Live'));
    socket.on('disconnect', () => setStatusPill(statusEl, 'danger', 'Offline'));
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

document.addEventListener('DOMContentLoaded', initThemeToggle);
