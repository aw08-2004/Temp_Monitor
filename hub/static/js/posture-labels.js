// Words for the security posture checks (roadmap #25 D), shared by the machine page's card,
// the device sheet and the Reports page. One copy, because three would drift, and a printed
// sheet that words a check differently from the screen it was printed from reads as two
// different findings.
//
// **Literal keys in switches, never built by concatenation.** tests/test_i18n.py can only
// scan calls whose key is a string literal, and a computed key whose translation was never written would
// ship silently and caption a row with its own key. The hub sends a check id, a status and a
// detail code (hub/posture.py); every one of them is mapped here by name.
//
// Detail parameters that are lists of codes (the firewall profiles) are translated here too;
// everything else in a parameter is text from the machine -- a product name, an account name
// -- and is passed through untouched for the caller to render with textContent.
(function () {
    'use strict';

    function checkTitle(id) {
        switch (id) {
            case 'antivirus': return t('posture.check.antivirus');
            case 'signatures': return t('posture.check.signatures');
            case 'autorun': return t('posture.check.autorun');
            case 'firewall': return t('posture.check.firewall');
            case 'session_lock': return t('posture.check.session_lock');
            case 'default_accounts': return t('posture.check.default_accounts');
            case 'admin_accounts': return t('posture.check.admin_accounts');
            case 'encryption': return t('posture.check.encryption');
            case 'secure_boot': return t('posture.check.secure_boot');
            case 'tpm': return t('posture.check.tpm');
            default: return id;
        }
    }

    function statusLabel(status) {
        if (status === 'pass') return t('posture.status.pass');
        if (status === 'fail') return t('posture.status.fail');
        return t('posture.status.unknown');
    }

    function profileName(code) {
        if (code === 'domain') return t('posture.profile.domain');
        if (code === 'private') return t('posture.profile.private');
        if (code === 'public') return t('posture.profile.public');
        return code;
    }

    function detailText(check) {
        const p = Object.assign({}, check.params || {});
        if (Array.isArray(p.profiles)) p.profiles = p.profiles.map(profileName).join(', ');
        switch (check.detail) {
            case 'read_failed': return t('posture.detail.read_failed', p);
            case 'av_on': return t('posture.detail.av_on', p);
            case 'av_off': return t('posture.detail.av_off', p);
            case 'av_none': return t('posture.detail.av_none', p);
            case 'sig_current': return t('posture.detail.sig_current', p);
            case 'sig_stale': return t('posture.detail.sig_stale', p);
            case 'sig_stale_product': return t('posture.detail.sig_stale_product', p);
            case 'autorun_off': return t('posture.detail.autorun_off', p);
            case 'autorun_on': return t('posture.detail.autorun_on', p);
            case 'fw_on': return t('posture.detail.fw_on', p);
            case 'fw_off': return t('posture.detail.fw_off', p);
            case 'lock_ok': return t('posture.detail.lock_ok', p);
            case 'lock_too_long': return t('posture.detail.lock_too_long', p);
            case 'lock_missing': return t('posture.detail.lock_missing', p);
            case 'lock_no_users': return t('posture.detail.lock_no_users', p);
            case 'defaults_disabled': return t('posture.detail.defaults_disabled', p);
            case 'defaults_enabled': return t('posture.detail.defaults_enabled', p);
            case 'admins_ok': return t('posture.detail.admins_ok', p);
            case 'admins_extra': return t('posture.detail.admins_extra', p);
            case 'enc_all': return t('posture.detail.enc_all', p);
            case 'enc_unprotected': return t('posture.detail.enc_unprotected', p);
            case 'enc_unsupported': return t('posture.detail.enc_unsupported', p);
            case 'enc_not_reported': return t('posture.detail.enc_not_reported', p);
            case 'secure_boot_on': return t('posture.detail.secure_boot_on', p);
            case 'secure_boot_off': return t('posture.detail.secure_boot_off', p);
            case 'secure_boot_legacy': return t('posture.detail.secure_boot_legacy', p);
            case 'tpm_ready': return t('posture.detail.tpm_ready', p);
            case 'tpm_missing': return t('posture.detail.tpm_missing', p);
            case 'tpm_not_ready': return t('posture.detail.tpm_not_ready', p);
            default: return t('posture.detail.not_reported', p);
        }
    }

    window.PostureLabels = { checkTitle, statusLabel, detailText };
}());
