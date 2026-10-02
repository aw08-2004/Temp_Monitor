// The device sheet (roadmap #25 A): everything the hub knows about one PC, drawn from
// GET /api/reports/machines/<machine>. One renderer, two homes -- the machine page's Report
// tab, and /reports/machines/<machine>, the page that actually gets printed. Two copies of the
// drawing would be two sheets that disagree about what a machine is.
//
// **Every section says WHY it is empty, never just that it is.** The hub sends a status per
// section (reports.py): `ok`, `waiting` or `not_collected`, and a `waiting_for` code naming
// what would fill it. A printed sheet outlives the moment it was printed, and a blank
// Software heading on paper reads as "nothing installed" to everybody who picks it up later.
// So a section with no data prints a sentence, and the sentence distinguishes "the machine
// has not checked in", "this machine's agent is too old to collect it" and "FleetHub does not
// collect this yet".
//
// **Everything the machine reported is text from the machine** -- a software name, a
// hostname, an adapter description -- and is rendered with textContent only. Nothing here
// builds HTML from data.
//
// Nothing polls. A sheet is a snapshot, and each section carries the time it was reported.
(function () {
    'use strict';

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    // Epoch seconds, or the text timestamps machine_info has always stored.
    function when(value) {
        if (value === null || value === undefined || value === '') return '';
        if (typeof value === 'number') return new Date(value * 1000).toLocaleString();
        return String(value);
    }

    function yesNo(value) {
        if (value === null || value === undefined) return '';
        return value ? t('report.yes') : t('report.no');
    }

    // Literal keys throughout, so tests/test_i18n.py can see every one of them. A label map
    // built by concatenation ('report.f.' + key) would ship a missing translation silently.
    function sectionTitle(id) {
        switch (id) {
            case 'identity': return t('report.section.identity');
            case 'os': return t('report.section.os');
            case 'directory': return t('report.section.directory');
            case 'fields': return t('report.section.fields');
            case 'groups': return t('report.section.groups');
            case 'hardware': return t('report.section.hardware');
            case 'firmware': return t('report.section.firmware');
            case 'volumes': return t('report.section.volumes');
            case 'network': return t('report.section.network');
            case 'security': return t('report.section.security');
            case 'software': return t('report.section.software');
            case 'apps': return t('report.section.apps');
            case 'patches': return t('report.section.patches');
            case 'sessions': return t('report.section.sessions');
            case 'capabilities': return t('report.section.capabilities');
            case 'location': return t('report.section.location');
            default: return id;
        }
    }

    function waitingText(code) {
        switch (code) {
            case 'agent_update': return t('report.waiting.agent_update');
            case 'phase_c': return t('report.waiting.phase_c');
            case 'directory': return t('report.waiting.directory');
            default: return t('report.waiting.agent_report');
        }
    }

    function labels() {
        return {
            manufacturer: t('report.f.manufacturer'),
            model: t('report.f.model'),
            serial_number: t('report.f.serial_number'),
            service_tag: t('report.f.service_tag'),
            asset_tag: t('report.f.asset_tag'),
            companion_version: t('report.f.agent_version'),
            update_channel: t('report.f.update_channel'),
            updated_at: t('report.f.last_report'),
            os_caption: t('report.f.os_caption'),
            os_version: t('report.f.os_version'),
            os_build: t('report.f.os_build'),
            os_arch: t('report.f.os_arch'),
            boot_epoch: t('report.f.last_boot'),
            last_uptime_seconds: t('report.f.uptime'),
            ad_dn: t('report.f.ad_dn'),
            ad_ou: t('report.f.ad_ou'),
            ad_owner: t('report.f.ad_owner'),
            ad_os: t('report.f.ad_os'),
            ad_last_logon: t('report.f.ad_last_logon'),
            ad_disabled: t('report.f.ad_disabled'),
            ad_synced_at: t('report.f.ad_synced_at'),
            cpu: t('report.f.cpu'),
            gpu: t('report.f.gpu'),
            memory_gb: t('report.f.memory_gb'),
            support: t('report.f.support'),
            vendor: t('report.f.vendor'),
            interface: t('report.f.interface'),
            bios_version: t('report.f.bios_version'),
            password_set: t('report.f.password_set'),
            error: t('report.f.error'),
            fast_startup: t('report.f.fast_startup'),
            bitlocker_support: t('report.f.bitlocker_support'),
            platform: t('report.f.platform'),
            lat: t('report.f.lat'),
            lon: t('report.f.lon'),
            accuracy_m: t('report.f.accuracy_m'),
            fixed_at: t('report.f.fixed_at'),
        };
    }

    // Which facts are times, which are booleans -- formatted rather than printed raw.
    const TIME_FIELDS = new Set(['boot_epoch', 'fixed_at', 'ad_synced_at', 'ad_last_logon']);
    const BOOL_FIELDS = new Set(['password_set', 'fast_startup', 'ad_disabled']);

    function factValue(key, value) {
        if (key === 'last_uptime_seconds' && typeof value === 'number' && window.formatUptime) {
            return window.formatUptime(value);
        }
        if (TIME_FIELDS.has(key)) return when(value);
        if (BOOL_FIELDS.has(key)) return yesNo(value);
        return value === null || value === undefined ? '' : String(value);
    }

    // A definition list of the facts that are present. An absent fact is left out rather
    // than printed as a blank row: twenty empty rows bury the three that say something.
    function facts(obj, keys) {
        const names = labels();
        const dl = el('dl', 'report-facts');
        (keys || Object.keys(obj || {})).forEach((key) => {
            const text = factValue(key, (obj || {})[key]);
            if (text === '') return;
            dl.appendChild(el('dt', null, names[key] || key));
            dl.appendChild(el('dd', null, text));
        });
        return dl.childElementCount ? dl : el('p', 'stat-card__meta', t('report.empty'));
    }

    function table(columns, rows) {
        if (!rows || rows.length === 0) return el('p', 'stat-card__meta', t('report.empty'));
        const wrap = el('div', 'report-table-wrap');
        const tbl = el('table', 'data-table');
        const head = el('tr');
        columns.forEach((c) => head.appendChild(el('th', null, c.label)));
        tbl.appendChild(el('thead')).appendChild(head);
        const body = el('tbody');
        rows.forEach((row) => {
            const tr = el('tr');
            columns.forEach((c) => tr.appendChild(el('td', null, c.get(row))));
            body.appendChild(tr);
        });
        tbl.appendChild(body);
        wrap.appendChild(tbl);
        return wrap;
    }

    function protectionLabel(state) {
        if (state === 'on') return t('bitlocker.protection.on');
        if (state === 'off') return t('bitlocker.protection.off');
        return t('bitlocker.protection.unknown');
    }

    function scopeLabel(scope) {
        return scope === 'user' ? t('report.scope.user') : t('report.scope.machine');
    }

    function drawBody(id, data) {
        switch (id) {
            case 'identity':
            case 'os':
            case 'directory':
            case 'firmware':
            case 'location':
                return facts(data);
            case 'fields': {
                const dl = el('dl', 'report-facts');
                Object.keys(data || {}).sort().forEach((name) => {
                    dl.appendChild(el('dt', null, name));
                    dl.appendChild(el('dd', null, data[name]));
                });
                return dl.childElementCount ? dl : el('p', 'stat-card__meta', t('report.no_fields'));
            }
            case 'groups':
                return data && data.length
                    ? el('p', null, data.join(', '))
                    : el('p', 'stat-card__meta', t('report.no_groups'));
            case 'hardware': {
                const box = el('div');
                box.appendChild(facts(data, ['cpu', 'gpu', 'memory_gb']));
                // Said on the sheet rather than left out: "can this PC take more RAM" is the
                // question a report gets asked most, and a sheet that is silent about DIMMs
                // reads as a PC that has none.
                box.appendChild(el('p', 'stat-card__meta', waitingText(data.detail_waiting_for)));
                return box;
            }
            case 'volumes':
                return table([
                    { label: t('report.col.volume'), get: (v) => v.name },
                    { label: t('report.col.used_gb'), get: (v) => v.used_gb === null || v.used_gb === undefined ? '' : v.used_gb.toFixed(1) },
                    { label: t('report.col.total_gb'), get: (v) => v.total_gb === null || v.total_gb === undefined ? '' : v.total_gb.toFixed(1) },
                    { label: t('report.col.used_pct'), get: (v) => v.used_pct === null || v.used_pct === undefined ? '' : `${Math.round(v.used_pct)}%` },
                ], data);
            case 'network': {
                const box = el('div');
                box.appendChild(table([
                    { label: t('report.col.adapter'), get: (n) => n.name || n.description },
                    { label: t('report.col.mac'), get: (n) => n.mac },
                    { label: t('report.col.ipv4'), get: (n) => n.ipv4 ? `${n.ipv4}/${n.prefix ?? ''}` : '' },
                    { label: t('report.col.kind'), get: (n) => n.kind },
                    { label: t('report.col.link'), get: (n) => yesNo(n.link_up) },
                ], data.nics));
                box.appendChild(facts(data, ['fast_startup']));
                return box;
            }
            case 'security': {
                const box = el('div');
                box.appendChild(facts(data, ['bitlocker_support', 'error']));
                box.appendChild(table([
                    { label: t('report.col.volume'), get: (v) => v.mount },
                    { label: t('report.col.protection'), get: (v) => protectionLabel(v.protection) },
                    { label: t('report.col.method'), get: (v) => v.method },
                    { label: t('report.col.recovery_password'), get: (v) => yesNo(v.has_recovery_password) },
                ], data.volumes));
                // The posture checks (#25 D), worded by the same PostureLabels the machine
                // page uses, so paper and screen cannot disagree about a result. A machine
                // whose agent predates them says so rather than printing no rows: a sheet
                // silent about the firewall reads as a PC whose firewall nobody questioned.
                if (data.checks && window.PostureLabels) {
                    const labels = window.PostureLabels;
                    box.appendChild(table([
                        { label: t('posture.col.check'), get: (c) => labels.checkTitle(c.id) },
                        { label: t('posture.col.result'), get: (c) => labels.statusLabel(c.status) },
                        { label: t('posture.col.detail'), get: (c) => labels.detailText(c) },
                        { label: t('posture.col.cis'), get: (c) => c.cis || '' },
                    ], data.checks));
                } else if (data.posture_waiting_for) {
                    box.appendChild(el('p', 'stat-card__meta', waitingText(data.posture_waiting_for)));
                }
                return box;
            }
            case 'software': {
                const box = el('div');
                const items = data.items || [];
                box.appendChild(el('p', 'stat-card__meta',
                    tPlural('report.software.count', items.length)));
                if (data.error) box.appendChild(el('p', 'stat-card__meta', data.error));
                box.appendChild(table([
                    { label: t('report.col.name'), get: (s) => s.name },
                    { label: t('report.col.version'), get: (s) => s.version },
                    { label: t('report.col.publisher'), get: (s) => s.publisher },
                    { label: t('report.col.installed'), get: (s) => s.install_date },
                    { label: t('report.col.scope'), get: (s) => scopeLabel(s.scope) },
                ], items));
                return box;
            }
            case 'apps':
                return table([
                    { label: t('report.col.name'), get: (a) => a.label },
                    { label: t('report.col.package'), get: (a) => a.package },
                    { label: t('report.col.version'), get: (a) => a.version },
                ], data);
            case 'patches':
                return table([
                    { label: t('report.col.title'), get: (p) => p.title },
                    { label: t('report.col.kb'), get: (p) => p.kb },
                    { label: t('report.col.source'), get: (p) => p.source },
                    { label: t('report.col.reboot'), get: (p) => yesNo(p.reboot_required) },
                ], data);
            case 'sessions':
                return el('p', null, t('report.sessions.summary', {
                    sessions: (data.sessions || []).length,
                }));
            case 'capabilities':
                return facts({ platform: data.platform }, ['platform']);
            default:
                return el('p', 'stat-card__meta', t('report.empty'));
        }
    }

    function draw(root, sheet) {
        root.replaceChildren();
        const head = el('div', 'report-sheet__head');
        head.appendChild(el('h2', 'report-sheet__title', sheet.machine));
        head.appendChild(el('p', 'stat-card__meta',
            t('report.generated', { when: when(sheet.generated_at) })));
        root.appendChild(head);

        const sections = sheet.sections || {};
        // `order`, not Object.keys: the hub's JSON arrives with its keys sorted alphabetically.
        (sheet.order || Object.keys(sections)).forEach((id) => {
            if (!sections[id]) return;
            const section = sections[id];
            const box = el('section', 'report-section card');
            const title = el('div', 'report-section__head');
            title.appendChild(el('h3', 'section-title', sectionTitle(id)));
            if (section.reported_at) {
                title.appendChild(el('span', 'stat-card__meta',
                    t('report.reported', { when: when(section.reported_at) })));
            }
            box.appendChild(title);
            if (section.status === 'ok') {
                box.appendChild(drawBody(id, section.data));
            } else {
                box.appendChild(el('p', 'stat-card__meta', waitingText(section.waiting_for)));
            }
            root.appendChild(box);
        });
    }

    async function load(machine, root, statusEl) {
        if (statusEl) statusEl.textContent = t('common.loading');
        let response;
        try {
            response = await fetch(`/api/reports/machines/${encodeURIComponent(machine)}`);
        } catch (e) {
            if (statusEl) statusEl.textContent = t('report.load_failed');
            return;
        }
        if (!response.ok) {
            if (statusEl) statusEl.textContent = t('report.load_failed');
            return;
        }
        draw(root, await response.json());
        if (statusEl) statusEl.textContent = '';
    }

    function exportLinks(machine) {
        const m = encodeURIComponent(machine);
        const json = document.querySelector('[data-report-export="json"]');
        const csv = document.querySelector('[data-report-export="software"]');
        if (json) json.href = `/api/reports/machines/${m}?download=1`;
        if (csv) csv.href = `/api/reports/export.csv?section=software&machines=${m}`;
    }

    window.ReportSheet = { load, draw };

    // Home one: the standalone, printable page.
    const standalone = document.getElementById('report-sheet-root');
    if (standalone && standalone.dataset.machine) {
        const machine = standalone.dataset.machine;
        exportLinks(machine);
        load(machine, standalone, document.getElementById('report-sheet-status'));
        const print = document.getElementById('report-print');
        if (print) print.addEventListener('click', () => window.print());
        return;
    }

    // Home two: the machine page's Report tab, drawn the first time the tab is shown. Not on
    // page load -- a sheet is a dozen reads, and most visits to a machine never open it.
    const panel = document.getElementById('tool-report');
    if (!panel || !window.MachineContext) return;
    const machine = window.MachineContext.current();
    if (!machine) return;
    exportLinks(machine);
    let loaded = false;
    panel.addEventListener('tab:shown', () => {
        if (loaded) return;
        loaded = true;
        load(machine, document.getElementById('report-tab-root'),
             document.getElementById('report-tab-status'));
    });
})();
