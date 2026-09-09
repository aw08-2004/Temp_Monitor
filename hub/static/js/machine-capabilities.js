// What the machine this page is about says it can do -- roadmap #23 phase F.2.
//
// A deliberate global, matching machine-context.js and fleet-api.js: no bundler, no module
// system.
//
// **Two questions, and the difference between them is the whole module.** Both are asked of the
// same report and they answer opposite ways when a machine has said nothing at all:
//
//   * `can(type)` -- may this machine still be OFFERED something the console has always
//     offered? A machine that has reported no capabilities is UNKNOWN, and unknown means yes.
//     Every Windows agent in the field reports nothing, and a console that hid Restart from all
//     of them the day this shipped would be a regression dressed as a feature. Same rule as
//     capabilities.py's absent-report rule, and for the same reason.
//   * `claims(type)` -- has this machine explicitly said it can do something NEW? Silence means
//     no. A Wipe button appearing on every Windows PC because none of them has said otherwise
//     is not a default anybody wants, and nothing is lost by waiting for the report: the
//     feature is new, so any machine that has it also reports it.
//
// The rule of thumb, written down because the next person will have to pick one: `can` for an
// action that predates capability reporting, `claims` for one that does not.
//
// **This is a courtesy, never a control.** Every route re-decides, and fleet.create_command
// refuses a command type the machine has disclaimed whatever the console drew. What this
// prevents is an operator clicking Terminal on a phone and reading "there is no shell for an
// app to run a script in" -- true, unhelpful, and avoidable.
//
// One request per page, shared: three cards asking the same question separately is three
// requests for one answer.
(function () {
    'use strict';

    let promise = null;
    let report = { platform: '', features: null, supported_commands: null };

    function load() {
        const machine = window.MachineContext && window.MachineContext.current();
        if (!machine) return Promise.resolve(report);
        if (promise) return promise;

        promise = fetch(`/api/machines/${encodeURIComponent(machine)}`)
            .then((response) => (response.ok ? response.json() : null))
            .then((detail) => {
                if (detail) {
                    report = {
                        platform: detail.platform || '',
                        // Null is preserved rather than defaulted to []: it is what "has not
                        // said" looks like, and the two questions above answer it differently.
                        features: detail.features || null,
                        supported_commands: detail.supported_commands || null,
                    };
                }
                return report;
            })
            .catch(() => report);
        return promise;
    }

    window.MachineCapabilities = {
        /** Resolves with {platform, features, supported_commands} once the report is in. */
        ready: load,

        /** May this machine be offered something the console has always offered? Unknown
         *  counts as yes -- see the note at the top. */
        can(type) {
            const list = report.supported_commands;
            return !Array.isArray(list) || list.indexOf(type) !== -1;
        },

        /** Has this machine explicitly claimed it can do this? Silence counts as no. */
        claims(type) {
            const list = report.supported_commands;
            return Array.isArray(list) && list.indexOf(type) !== -1;
        },

        /** Has this machine explicitly claimed a non-command feature (locate, app_policy,
         *  usage_access, device_owner)? Silence counts as no, same as claims(). */
        hasFeature(name) {
            const list = report.features;
            return Array.isArray(list) && list.indexOf(name) !== -1;
        },

        /** The platform slug, or '' when the machine has not said. Never derived from the OS
         *  caption: that is a fuzzy display bucket over a string a remote machine chose, and
         *  hiding a button on a substring of it is how the wrong machine loses a feature. */
        platform() {
            return report.platform;
        },
    };
})();
