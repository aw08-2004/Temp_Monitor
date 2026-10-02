// The app shell: the sidebar/topbar document that never reloads, and the frames that pages
// live in inside it.
//
// WHY. Remote view is a WebRTC session, and a WebRTC session belongs to the document that
// negotiated it. In a plain multi-page app -- which this was -- clicking Packages unloads the
// document, so remote.js stops its sessions on pagehide (leaving them would strand a capture
// helper and a live TURN credential on the target PC for the session's whole TTL). Watching a
// PC therefore meant not leaving the Remote page. The fix is to stop unloading the document:
// the chrome stays, and pages are navigated inside a frame under it.
//
// TWO KINDS OF FRAME.
//   * ONE TRANSIENT FRAME, reused for every ordinary page. Navigating it is a real page load,
//     exactly as before -- Packages does not need to survive being left, and keeping ten
//     pages alive would keep ten pages polling.
//   * ONE PERSISTENT FRAME PER PATH IN `PERSISTENT`, created on first visit and thereafter
//     only hidden. That is what keeps remote screens connected across a trip to Packages, and
//     it is the list to add to when another page earns the same treatment.
//
// THE ASSISTANT IS THE SECOND PERSISTENT FRAME, and the one frame that can be on screen at the
// same time as another (roadmap #26). Opened from the sidebar it is a section like any other;
// PINNED, it is docked to the right of whatever page is showing and stays there across
// navigation -- the same iframe, moved by a class, so the conversation in it is never
// reloaded. Rejected: a second, docked copy of the chat UI in this document. Two
// implementations of one panel drift, and this document deliberately renders no page content
// (base.html loads no page stylesheet into it).
//
// The fleet consoles are deliberately NOT in that list: a PTY session already outlives its
// page on the hub and re-attaches on return (see fleet-pty.js), so a frame kept alive for
// them would cost memory to duplicate something the server already does properly.
//
// URLS ARE REAL. Every navigation lands on the same url the old link went to, pushed with
// the History API, and the server answers that url with the shell or with the bare page
// depending on Sec-Fetch-Dest (see app.py's _shell_mode). So bookmarks, refresh, deep links,
// Back and ctrl-click all behave, and no template needed a new href.
//
// IIFE-wrapped, classic script, no bundler -- same as every other file in static/js.
(function () {
    'use strict';

    const host = document.getElementById('app-frames');
    if (!host) return;

    /** Paths whose frame is kept alive in the background. A path matches if it is equal to
     *  the entry or nested under it. */
    const PERSISTENT = ['/remote', '/assistant'];

    /** The persistent frame that can be docked beside the others. */
    const ASSISTANT = '/assistant';
    const PIN_KEY = 'tempmonitor:assistant';
    const WIDTH_KEY = 'tempmonitor:assistant:width';
    const MIN_WIDTH = 320;
    const MAX_WIDTH = 900;
    // Docking needs a page left over beside the panel. Below this the panel is a section and
    // nothing else, the same breakpoint the sidebar turns into a drawer at.
    const narrow = window.matchMedia('(max-width: 900px)');

    /** Paths that must replace the shell rather than load inside it: they are not app pages.
     *  Signing out inside a frame would leave the chrome of a signed-in session wrapped
     *  around a login form, and Google refuses to be framed at all. */
    const TOP_LEVEL = ['/login', '/logout'];

    /** The key every non-persistent page shares -- they take turns in one frame. Not a path,
     *  so it can never collide with one. */
    const TRANSIENT = 'transient';

    /** key -> iframe. */
    const frames = new Map();
    let visible = null;
    /** The last frame shown that was not the assistant -- what "the page I am on" means to
     *  the assistant, and where pinning from the assistant's own section goes back to. */
    let lastPage = null;
    let pinned = readPinned();
    let handle = null;

    function readPinned() {
        try { return localStorage.getItem(PIN_KEY) === 'pinned'; } catch (e) { return false; }
    }

    function savePinned() {
        try { localStorage.setItem(PIN_KEY, pinned ? 'pinned' : 'open'); } catch (e) { /* ok */ }
    }

    function readWidth() {
        let width = 420;
        try { width = parseInt(localStorage.getItem(WIDTH_KEY), 10) || width; } catch (e) { /* ok */ }
        return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width));
    }

    // ---------------- Small helpers ----------------
    function startsWithPath(path, prefix) {
        if (prefix === '/') return path === '/';
        return path === prefix || path.startsWith(prefix + '/');
    }

    function keyFor(path) {
        for (const prefix of PERSISTENT) {
            if (startsWithPath(path, prefix)) return prefix;
        }
        return TRANSIENT;
    }

    /** Where a frame actually is. Same-origin, so this is readable -- but a page that
     *  redirected off-origin would throw, and the src is the best answer left. */
    function frameHref(frame) {
        try {
            const href = frame.contentWindow.location.href;
            return href === 'about:blank' ? frame.src : href;
        } catch (e) {
            return frame.src;
        }
    }

    function frameDoc(frame) {
        try {
            return frame.contentDocument;
        } catch (e) {
            return null;
        }
    }

    /** Move an existing frame. location.replace() rather than a src assignment: replace adds
     *  no entry to the browser's joint session history, so the only history the Back button
     *  walks is the one this file pushes -- and the shell and the url bar cannot drift apart.
     *  The fallback is for a frame sitting on a document we may no longer touch. */
    function go(frame, href) {
        try {
            frame.contentWindow.location.replace(href);
        } catch (e) {
            frame.src = href;
        }
    }

    // ---------------- Chrome that tracks the frame ----------------
    function syncNav(path) {
        for (const link of document.querySelectorAll('.sidebar__link[data-nav-prefix]')) {
            const prefixes = link.dataset.navPrefix.split(/\s+/);
            // A docked assistant is open whatever the page, so its link stays lit.
            const active = prefixes.some((prefix) => startsWithPath(path, prefix))
                || (prefixes.includes(ASSISTANT) && !!docked());
            link.classList.toggle('sidebar__link--active', active);
            if (active) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        }
    }

    /** The topbar's live-data pill is shared by every page in the shell, so it follows
     *  whichever frame claimed it -- see connectSocketWithStatus in common.js, which sets the
     *  mark from inside. Checking the mark rather than the path means a page that stops
     *  using a socket needs nothing changed here. */
    function syncSocketPill() {
        const pill = document.getElementById('socket-status');
        if (pill) pill.hidden = !visible || visible.dataset.socket !== 'yes';
    }

    function applyTheme(frame) {
        const doc = frameDoc(frame);
        if (!doc) return;
        doc.documentElement.setAttribute(
            'data-theme', document.documentElement.getAttribute('data-theme'));
    }

    // ---------------- Frames ----------------
    function makeFrame(href) {
        const frame = document.createElement('iframe');
        frame.className = 'app-frames__frame';
        // The generic label only stands until the page inside names itself on load; an
        // untitled frame is announced as nothing more useful than "frame".
        frame.title = t('shell.frame_title');
        // Fullscreen is delegated explicitly or the remote viewer's Fullscreen button does
        // nothing at all: a framed document may not enter fullscreen without it.
        frame.setAttribute('allow', 'fullscreen; clipboard-read; clipboard-write');
        frame.setAttribute('allowfullscreen', '');
        // src BEFORE insertion, so this load is the frame's INITIAL navigation. A later src
        // assignment would push an entry onto the browser's joint session history and put
        // Back one step out of sync with the shell; location.replace() below is the reason
        // every subsequent navigation does not.
        frame.src = href;
        adopt(frame);
        host.appendChild(frame);
        return frame;
    }

    /** Wire a frame's load event once. Fires on every navigation the frame makes, including
     *  ones the shell did not start (a row click that assigns location.href), which is
     *  exactly why the url bar is synced from here rather than from navigate(). */
    function adopt(frame) {
        frame.addEventListener('load', () => {
            applyTheme(frame);
            const doc = frameDoc(frame);
            frame.__fleetContext = null;
            if (frame !== frames.get(ASSISTANT)) notifyAssistant();
            if (doc) {
                interceptLinks(doc);
                // Ctrl+K pressed inside a page never reaches this document, so the palette
                // listens on each framed document too (command-palette.js).
                if (window.FleetPalette) window.FleetPalette.listen(doc);
                frame.title = doc.title || frame.title;
            }
            if (frame !== visible) return;
            const href = frameHref(frame);
            if (href !== location.href) history.replaceState(history.state, '', href);
            if (doc && doc.title) document.title = doc.title;
            syncNav(new URL(href, location.href).pathname);
            syncSocketPill();
        });
    }

    function show(frame, { focus = true } = {}) {
        visible = frame;
        const dock = docked();
        for (const other of frames.values()) other.hidden = other !== frame && other !== dock;
        if (frame !== frames.get(ASSISTANT)) lastPage = frame;
        const doc = frameDoc(frame);
        if (doc && doc.title) document.title = doc.title;
        syncSocketPill();
        // Hand the keyboard to the page that just came to the front: a remote screen that
        // needs a click on its own picture before it takes typing is a remote screen that
        // looks broken.
        if (focus) {
            try { frame.contentWindow.focus(); } catch (e) { /* not ours to focus */ }
        }
    }

    // ---------------- The docked assistant ----------------
    /** The assistant frame when it is docked right now, else null. */
    function docked() {
        if (!pinned || narrow.matches) return null;
        const frame = frames.get(ASSISTANT);
        return frame && frame !== visible ? frame : null;
    }

    function ensureHandle() {
        if (handle) return handle;
        handle = document.createElement('div');
        handle.className = 'app-frames__dock-handle';
        handle.setAttribute('role', 'separator');
        handle.setAttribute('aria-orientation', 'vertical');
        handle.setAttribute('aria-label', t('assistant.resize'));
        handle.hidden = true;
        handle.addEventListener('pointerdown', startResize);
        host.appendChild(handle);
        return handle;
    }

    /** Dragging over an iframe hands the pointer to the document inside it, and the drag
     *  stops dead the moment it crosses the panel. So every frame stops taking pointer events
     *  for the length of the drag (the --resizing class), and the drag is tracked out here. */
    function startResize(e) {
        e.preventDefault();
        host.classList.add('app-frames--resizing');
        const move = (ev) => {
            const right = host.getBoundingClientRect().right;
            const width = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, right - ev.clientX));
            host.style.setProperty('--assistant-width', `${width}px`);
        };
        const up = () => {
            host.classList.remove('app-frames--resizing');
            window.removeEventListener('pointermove', move);
            window.removeEventListener('pointerup', up);
            window.removeEventListener('pointercancel', up);
            const width = parseInt(host.style.getPropertyValue('--assistant-width'), 10);
            try { if (width) localStorage.setItem(WIDTH_KEY, String(width)); } catch (err) { /* ok */ }
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
        // A touch or pen drag can end in pointercancel rather than pointerup, and missing it
        // leaves every frame at pointer-events: none until the page is reloaded.
        window.addEventListener('pointercancel', up);
    }

    /** Put the assistant frame where the pin state says, without touching anything else. */
    function applyDock() {
        const frame = frames.get(ASSISTANT);
        const dock = docked();
        ensureHandle().hidden = !dock;
        if (frame) {
            frame.classList.toggle('app-frames__frame--dock', !!dock);
            if (frame !== visible) frame.hidden = !dock;
        }
        host.classList.toggle('app-frames--docked', !!dock);
        host.style.setProperty('--assistant-width', `${readWidth()}px`);
        syncNav(new URL(location.href).pathname);
    }

    function assistantFrame() {
        let frame = frames.get(ASSISTANT);
        if (!frame) {
            frame = makeFrame(ASSISTANT);
            frame.hidden = true;
            frames.set(ASSISTANT, frame);
        }
        return frame;
    }

    function pin() {
        if (narrow.matches) return;
        pinned = true;
        savePinned();
        const frame = assistantFrame();
        if (visible === frame) {
            // Pinned from its own section: put the page it was opened over back beside it.
            if (lastPage && lastPage !== frame) {
                show(lastPage, { focus: false });
                const href = frameHref(lastPage);
                if (href !== location.href) history.pushState({}, '', href);
            } else {
                navigate('/', { push: true, focus: false });
            }
        }
        applyDock();
    }

    function unpin() {
        pinned = false;
        savePinned();
        applyDock();
    }

    /** What the operator is looking at, for the assistant. The visible page, or -- while the
     *  assistant is open as a section -- the page it was opened from. */
    function pageContext() {
        const assistant = frames.get(ASSISTANT);
        const frame = visible && visible !== assistant ? visible : lastPage;
        if (!frame || frame === assistant) return { path: ASSISTANT };
        const url = new URL(frameHref(frame), location.href);
        const context = { path: url.pathname, query: url.search.slice(1), hash: url.hash.slice(1) };
        const doc = frameDoc(frame);
        if (doc && doc.title) context.title = doc.title;
        const machine = url.pathname.match(/^\/(?:machine|reports\/machines)\/([^/]+)/);
        if (machine) context.machine = decodeURIComponent(machine[1]);
        else if (url.searchParams.get('machine')) context.machine = url.searchParams.get('machine');
        if (url.searchParams.get('tab')) context.tab = url.searchParams.get('tab');
        return Object.assign(context, frame.__fleetContext || {});
    }

    /** Called by a page (through window.parent) to add what its URL cannot say -- a
     *  selection, an open folder. Cleared when that frame navigates. */
    function setContext(win, extra) {
        for (const frame of frames.values()) {
            if (frame.contentWindow === win) frame.__fleetContext = Object.assign({}, extra || {});
        }
        notifyAssistant();
    }

    function notifyAssistant() {
        const frame = frames.get(ASSISTANT);
        if (!frame) return;
        try {
            frame.contentWindow.dispatchEvent(new CustomEvent('fleet:pagechange'));
        } catch (e) { /* still loading */ }
    }

    // ---------------- Navigation ----------------
    function navigate(href, { push = false, focus = true } = {}) {
        const url = new URL(href, location.href);
        const key = keyFor(url.pathname);
        // Already docked: the sidebar link and every [[page:assistant]] link mean "go to the
        // assistant", and it is right there. Moving it into the page area would unpin it.
        if (key === ASSISTANT && docked()) {
            try { frames.get(ASSISTANT).contentWindow.focus(); } catch (e) { /* ok */ }
            return;
        }
        let frame = frames.get(key);
        let landed = url.href;

        if (!frame) {
            frame = makeFrame(url.href);
            frames.set(key, frame);
        } else if (key === TRANSIENT) {
            go(frame, url.href);
        } else if (url.search || url.hash) {
            // A persistent frame is reloaded only when the url asks for something specific.
            // Bare /remote means "show me my screens" and must never reload; /remote?machine=
            // PC-2 is a request to open something, and is worth the reload it costs.
            if (frameHref(frame) !== url.href) go(frame, url.href);
        } else {
            // Reusing it as it stands, so the url bar should say where it actually is --
            // clicking Remote with PC-2 open belongs on /remote?machine=PC-2, not on a bare
            // /remote that would reopen nothing if it were reloaded.
            landed = frameHref(frame) || url.href;
        }

        show(frame, { focus });
        if (push && landed !== location.href) history.pushState({}, '', landed);
        else if (!push) history.replaceState(history.state, '', landed);
        applyDock();
        syncNav(new URL(landed, location.href).pathname);
    }

    /** Left-clicks on same-origin links move the frame instead of the shell. Everything that
     *  is not a plain left-click on a plain link is left entirely alone: modified clicks and
     *  middle-clicks still open tabs, target=_blank still opens windows, downloads still
     *  download, and a handler that already called preventDefault still wins. */
    function onClick(e) {
        if (e.defaultPrevented || e.button !== 0) return;
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        const link = e.target.closest && e.target.closest('a[href]');
        if (!link || link.hasAttribute('download')) return;
        if (link.target && link.target !== '_self') return;

        let url;
        try {
            url = new URL(link.href, link.baseURI);
        } catch (err) {
            return;
        }
        if (url.origin !== location.origin) return;
        if (TOP_LEVEL.some((path) => startsWithPath(url.pathname, path))) {
            // Let it happen, but to the whole window rather than inside a frame.
            if (link.ownerDocument !== document) {
                e.preventDefault();
                window.location.href = url.href;
            }
            return;
        }
        e.preventDefault();
        navigate(url.href, { push: true });
    }

    /** The same handler, inside a frame's document. Links in a page then route through the
     *  shell too, which is what lets a link to /remote land on the LIVE remote frame instead
     *  of loading a second copy of it inside the transient one. */
    function interceptLinks(doc) {
        if (doc.__shellLinks) return;
        doc.__shellLinks = true;
        doc.addEventListener('click', onClick);
    }

    document.addEventListener('click', onClick);

    window.addEventListener('popstate', () => {
        // A frame that navigated itself (a row click that assigns location.href) does put an
        // entry in the browser's joint history, and walking back over one of those moves the
        // frame on its own -- by the time the url bar says what the frame is already showing,
        // there is nothing left for the shell to do.
        if (visible && frameHref(visible) === location.href) return;
        navigate(location.href, { push: false, focus: false });
    });

    // The toggle lives out here in the chrome, so the frames have to be told. Their own boot
    // script reads the same localStorage key, which covers a frame that loads later.
    document.addEventListener('theme:change', () => {
        for (const frame of frames.values()) applyTheme(frame);
    });

    // ---------------- Boot ----------------
    // The first frame is rendered by base.html rather than created here, so the page inside
    // it starts loading with the document instead of a script later. Adopt it as it stands.
    window.FleetShell = {
        pageContext, setContext, pin, unpin,
        isPinned: () => !!docked(),
        canPin: () => !narrow.matches,
    };

    const first = host.querySelector('.app-frames__frame');
    if (first) {
        adopt(first);
        frames.set(keyFor(location.pathname), first);
        show(first, { focus: false });
    }
    // Opening /assistant itself shows it as the section it asked for; the pin comes back the
    // next time a page is shown beside it.
    if (pinned && keyFor(location.pathname) !== ASSISTANT) assistantFrame();
    narrow.addEventListener('change', applyDock);
    applyDock();
    syncNav(location.pathname);
})();
