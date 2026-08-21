// The Dashboard: the fleet, not the machines in it.
//
// This file used to render one card per machine from /api/machines. That made the front
// page a second, worse Asset Inventory -- the same list with less on it -- so it asks a
// different question now: how many machines, on what, how hot, and what is happening. One
// GET /api/fleet/summary supplies all of it, deliberately as ONE request: the numbers have
// to agree with each other, and eight requests cannot be made to agree on a moment.
//
// Two rules carried over from the version this replaces, and one dropped.
//
//  * EVERY value is written with createElement/textContent, never innerHTML. Machine names
//    reach the hub through /api/report, which is unauthenticated by design, and OS captions
//    come from the same place -- so both are arbitrary attacker-controlled text. This page
//    now draws more of that text than it ever did, not less.
//  * It still does NOT flag high temperatures as a fault. What counts as too hot is an
//    operator-written rule (see rules.py), surfacing in Alerts. The tile here says "over
//    N C" with N on screen, so it reads as the filter it is rather than as a verdict.
//  * DROPPED: the socket.io connection. With no per-machine cards there is nothing for a
//    `new_temp` event to update, and re-fetching a fleet aggregate at 1 Hz is not viable.
//    The live-data pill stays dark here, which is honest -- this is a 30-second poll.
(function () {
    'use strict';

    const REFRESH_MS = 30_000;

    const errorEl = document.getElementById('summary-error');
    const generatedEl = document.getElementById('health-generated');
    const healthEl = document.getElementById('health-tiles');
    const osEl = document.getElementById('os-breakdown');
    const telemetryEl = document.getElementById('telemetry-tiles');
    const activityEl = document.getElementById('activity-tiles');
    const alertsEl = document.getElementById('activity-alerts');
    const attentionEl = document.getElementById('attention-lists');
    if (!healthEl) return;

    // A switch of literal keys rather than one key built by concatenating the bucket name
    // onto a prefix. A computed key is invisible to tests/test_i18n.py, which can only scan
    // for literals -- so a bucket whose translation was never written would ship silently
    // and show a raw key. Same reason common.js spells out its status labels one by one.
    function osBucketLabel(bucket) {
        switch (bucket) {
            case 'windows_11': return t('dashboard.os.bucket.windows_11');
            case 'windows_10': return t('dashboard.os.bucket.windows_10');
            case 'windows_server': return t('dashboard.os.bucket.windows_server');
            case 'linux': return t('dashboard.os.bucket.linux');
            default: return t('dashboard.os.bucket.unknown');
        }
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    const DASH = '--';

    function num(value) {
        return (value === null || value === undefined) ? DASH : String(value);
    }

    /**
     * One number with a label under it.
     *
     * `tone` colours the number and is used sparingly: a count of zero failures must not be
     * red just because the tile is about failures, so callers pass a tone only when the
     * value itself is the news.
     */
    function tile(label, value, { tone = null, hint = null } = {}) {
        const node = el('div', 'tile');
        const v = el('div', 'tile__value', num(value));
        if (tone) v.classList.add(`tile__value--${tone}`);
        node.append(v, el('div', 'tile__label', label));
        if (hint) node.appendChild(el('div', 'tile__hint', hint));
        return node;
    }

    function renderHealth(summary) {
        const c = summary.counts;
        healthEl.replaceChildren(
            tile(t('dashboard.health.total'), c.total),
            tile(t('dashboard.health.online'), c.online, { tone: c.online ? 'ok' : null }),
            tile(t('dashboard.health.offline'), c.offline,
                 { tone: c.offline ? 'warn' : null }),
            // Not a fault, but the thing that most often explains "why did nothing happen
            // when I clicked that": an unenrolled agent reports telemetry and accepts no
            // commands, so it looks perfectly healthy and does nothing.
            tile(t('dashboard.health.never_enrolled'), c.never_enrolled,
                 { tone: c.never_enrolled ? 'warn' : null,
                   hint: t('dashboard.health.never_enrolled_hint') }),
            tile(t('dashboard.health.open_alerts'), c.open_alerts,
                 { tone: c.open_alerts ? 'warn' : null }),
            tile(t('dashboard.health.agents_outdated'), c.agents_outdated,
                 { hint: c.agent_latest
                     ? t('dashboard.health.agent_latest', { version: c.agent_latest })
                     : null })
        );
        generatedEl.textContent = summary.generated_at
            ? t('dashboard.generated_at', {
                time: new Date(summary.generated_at * 1000).toLocaleTimeString() })
            : '';
    }

    function renderOs(summary) {
        const total = summary.counts.total || 0;
        osEl.replaceChildren();
        if (!total) {
            osEl.appendChild(el('p', 'stat-card__meta', t('dashboard.empty')));
            return;
        }
        for (const entry of summary.os) {
            // Buckets nobody has are dropped rather than shown as zero: a fleet with no
            // Linux does not need a permanent Linux row, and the list stays as long as the
            // fleet is varied.
            if (!entry.count) continue;
            const row = el('div', 'breakdown__row');
            row.appendChild(el('span', 'breakdown__label', osBucketLabel(entry.bucket)));
            const barWrap = el('span', 'breakdown__bar');
            const bar = el('span', 'breakdown__fill');
            bar.style.width = `${Math.round(100 * entry.count / total)}%`;
            barWrap.appendChild(bar);
            row.appendChild(barWrap);
            row.appendChild(el('span', 'breakdown__count',
                               t('dashboard.os.count', { count: entry.count,
                                                         pct: Math.round(100 * entry.count / total) })));
            osEl.appendChild(row);
        }
    }

    function renderTelemetry(summary) {
        const s = summary.telemetry;
        const gb = (value) => (value === null || value === undefined || !value)
            ? DASH : t('dashboard.telemetry.gb', { value: Math.round(value) });
        telemetryEl.replaceChildren(
            tile(t('dashboard.telemetry.avg_temp'),
                 s.avg_cpu_temp === null ? DASH : t('dashboard.telemetry.celsius', { value: s.avg_cpu_temp })),
            tile(t('dashboard.telemetry.peak_temp'),
                 s.peak_cpu_temp === null ? DASH : t('dashboard.telemetry.celsius', { value: s.peak_cpu_temp })),
            // The threshold is IN the label, not implied by it: this is a filter somebody
            // chose in Settings, not the hub declaring these machines faulty.
            tile(t('dashboard.telemetry.over_threshold', { threshold: s.threshold_c }),
                 s.over_threshold, { tone: s.over_threshold ? 'warn' : null }),
            tile(t('dashboard.telemetry.avg_load'),
                 s.avg_cpu_load_pct === null ? DASH : t('dashboard.telemetry.percent', { value: s.avg_cpu_load_pct })),
            tile(t('dashboard.telemetry.disk_free'), gb(s.disk_free_gb),
                 { hint: s.disk_total_gb ? t('dashboard.telemetry.of_total', { value: gb(s.disk_total_gb) }) : null }),
            tile(t('dashboard.telemetry.low_disk', { threshold: s.low_disk_free_pct }),
                 s.low_disk_machines, { tone: s.low_disk_machines ? 'warn' : null }),
            // Says how much of the fleet these averages are actually OF. An average over
            // three of two hundred machines is not a fleet average, and a page that showed
            // it without saying so would be quietly lying.
            tile(t('dashboard.telemetry.reporting'), s.reporting,
                 { hint: t('dashboard.telemetry.reporting_hint') })
        );
    }

    function renderActivity(summary) {
        const a = summary.activity;
        activityEl.replaceChildren(
            tile(t('dashboard.activity.deployments_running'), a.deployments_running),
            tile(t('dashboard.activity.deployments_failed'), a.deployments_failed_24h,
                 { tone: a.deployments_failed_24h ? 'bad' : null }),
            tile(t('dashboard.activity.backups_ok'), a.backups_ok_24h),
            tile(t('dashboard.activity.backups_failed'), a.backups_failed_24h,
                 { tone: a.backups_failed_24h ? 'bad' : null }),
            tile(t('dashboard.activity.firmware_jobs'), a.firmware_jobs_active),
            tile(t('dashboard.activity.wake_requests'), a.wake_requests_open)
        );

        alertsEl.replaceChildren();
        if (!a.alerts_recent.length) return;
        alertsEl.appendChild(el('div', 'section-title dashboard__subhead',
                                t('dashboard.activity.alerts_title')));
        const list = el('div', 'attention-list');
        for (const alert of a.alerts_recent) {
            const row = el('div', 'attention-list__row');
            if (alert.machine) {
                const link = el('a', 'attention-list__name', alert.machine);
                link.href = '/machine/' + encodeURIComponent(alert.machine);
                row.appendChild(link);
            } else {
                // A fleet-wide alert (a duplicate serial spanning several machines) has no
                // single subject to link to.
                row.appendChild(el('span', 'attention-list__name', t('dashboard.activity.fleet_wide')));
            }
            row.appendChild(el('span', 'attention-list__value', alert.kind));
            list.appendChild(row);
        }
        alertsEl.appendChild(list);
    }

    /** One ranked list: a title and up to `top` rows, each linking to its machine. */
    function attentionList(title, rows, valueFor, emptyText) {
        const box = el('div', 'attention');
        box.appendChild(el('div', 'section-title dashboard__subhead', title));
        if (!rows.length) {
            box.appendChild(el('p', 'stat-card__meta', emptyText));
            return box;
        }
        const list = el('div', 'attention-list');
        for (const row of rows) {
            const line = el('div', 'attention-list__row');
            const link = el('a', 'attention-list__name', row.machine);
            link.href = '/machine/' + encodeURIComponent(row.machine);
            line.append(link, el('span', 'attention-list__value', valueFor(row)));
            list.appendChild(line);
        }
        box.appendChild(list);
        return box;
    }

    function humanGap(seconds) {
        if (seconds === null || seconds === undefined) return t('dashboard.attention.never');
        const days = Math.floor(seconds / 86400);
        if (days >= 1) return t('dashboard.attention.days', { count: days });
        const hours = Math.floor(seconds / 3600);
        if (hours >= 1) return t('dashboard.attention.hours', { count: hours });
        return t('dashboard.attention.minutes', { count: Math.max(1, Math.floor(seconds / 60)) });
    }

    function renderAttention(summary) {
        const a = summary.attention;
        attentionEl.replaceChildren(
            attentionList(t('dashboard.attention.hottest'), a.hottest,
                          (r) => t('dashboard.telemetry.celsius', { value: r.temp }),
                          t('dashboard.attention.no_readings')),
            attentionList(t('dashboard.attention.low_disk'), a.low_disk,
                          (r) => t('dashboard.attention.free_pct', { pct: r.free_pct, gb: r.free_gb }),
                          t('dashboard.attention.no_disks')),
            attentionList(t('dashboard.attention.longest_offline'), a.longest_offline,
                          (r) => humanGap(r.seconds),
                          t('dashboard.attention.all_online'))
        );
    }

    async function refresh() {
        let summary;
        try {
            const response = await fetch('/api/fleet/summary');
            if (!response.ok) throw new Error(t('common.hub_error', { status: response.status }));
            summary = await response.json();
        } catch (e) {
            // The last good numbers stay on screen. A page that blanked itself on one failed
            // poll would be less useful than one showing figures thirty seconds old, and the
            // banner says which it is.
            errorEl.hidden = false;
            errorEl.textContent = e.message;
            return;
        }
        errorEl.hidden = true;
        renderHealth(summary);
        renderOs(summary);
        renderTelemetry(summary);
        renderActivity(summary);
        renderAttention(summary);
    }

    refresh();
    setInterval(refresh, REFRESH_MS);
    // Catch up immediately on returning to the tab rather than waiting out the rest of an
    // interval that ticked away while nobody was looking.
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refresh();
    });
})();
