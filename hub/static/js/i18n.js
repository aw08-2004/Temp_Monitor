// Browser half of the translation system (roadmap #7). Loaded before every other script
// in base.html, because the modules that follow call t() at import time.
//
// The catalog is the SAME merged dict the server used to render the page, embedded in
// #i18n-catalog rather than fetched. Two reasons: a fetch would race the scripts that
// call t() during their own top-level execution, and an endpoint would need cache
// busting on every hub update and every language switch -- an embedded catalog is always
// exactly as fresh as the page around it.
//
// Fallback to English already happened server-side (i18n.catalog_for merges the two), so
// there is deliberately no fallback logic here. One implementation, one behaviour: a
// second one on this side could drift from the Python and produce a page that translates
// differently depending on whether Jinja or JS rendered the string.
(function () {
    'use strict';

    var catalog = {};
    try {
        var el = document.getElementById('i18n-catalog');
        if (el) catalog = JSON.parse(el.textContent) || {};
    } catch (e) {
        // A broken catalog must not take the console down with it -- t() below then
        // returns key names, which is visibly wrong but still navigable.
        catalog = {};
    }

    var lang = document.documentElement.getAttribute('lang') || 'en';

    // Mirrors i18n._SafeFormat: an unmatched {placeholder} renders as itself rather than
    // as "undefined". Same reasoning -- a translator's typo should be visible and
    // survivable, not a broken string on an operator's screen.
    function interpolate(text, params) {
        if (!params) return text;
        return text.replace(/\{(\w+)\}/g, function (whole, name) {
            return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : whole;
        });
    }

    // Unknown key -> the key itself. Deliberately ugly: it is a typo in the calling code,
    // it is caught by tests/test_i18n.py's key scan, and rendering blank would hide it.
    function t(key, params) {
        var text = catalog[key];
        if (text === undefined) return key;
        return interpolate(text, params);
    }

    // Matches i18n.plural(): one/other, split at exactly 1. Correct for en/es/de and
    // nothing else -- see the note in hub/i18n.py before adding a language.
    function tPlural(key, count, params) {
        var merged = { count: count };
        if (params) {
            for (var k in params) {
                if (Object.prototype.hasOwnProperty.call(params, k)) merged[k] = params[k];
            }
        }
        return t(key + (count === 1 ? '.one' : '.other'), merged);
    }

    window.t = t;
    window.tPlural = tPlural;
    window.HUB_LANG = lang;

    // ---- language picker (topbar) ------------------------------------------------
    // A full reload rather than re-rendering in place: every server-rendered string on
    // the page came from the old catalog, so swapping only the JS-built ones would leave
    // a half-translated screen. The preference is persisted server-side first, so the
    // reload comes back in the new language.
    document.addEventListener('DOMContentLoaded', function () {
        var picker = document.getElementById('language-picker');
        if (!picker) return;
        picker.addEventListener('change', function () {
            var chosen = picker.value;
            picker.disabled = true;
            fetch('/api/language', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ language: chosen })
            }).then(function (resp) {
                if (!resp.ok) throw new Error('http ' + resp.status);
                window.location.reload();
            }).catch(function () {
                picker.disabled = false;
                picker.value = lang;
                alert(t('common.language_change_failed'));
            });
        });
    });
})();
