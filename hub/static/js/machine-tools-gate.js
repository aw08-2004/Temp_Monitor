// Hide what a machine has told us it cannot answer -- roadmap #23 phase F.2.
//
// **The problem this closes is a console that offers work and then explains why it failed.**
// The Terminal, Backup, Firmware, Network and Files links, and the Processes card, have always
// been drawn for every machine, because until capability reporting there was nothing to draw
// them from: an agent's VERSION can say "too old for this yet", which is a sentence about time,
// and cannot say "this device will never have a shell", which is a fact about the platform. On
// a phone all six are the second kind, and clicking one produced a correct and useless refusal
// from the agent's dispatcher.
//
// **Silence keeps everything.** A machine that has reported no capabilities is unknown, not
// incapable -- see machine-capabilities.js. Every Windows agent in the field reports nothing,
// and this module must be invisible to all of them.
//
// **And it says so rather than just shortening the row.** A toolbar with three buttons where
// the last machine had six reads as a page that failed to load. One sentence naming the
// platform turns it into a fact about the device.
//
// Anything with `data-needs-command` anywhere on the page is gated; the sentence is only about
// the ones in the tools row, because that is the row whose length changes visibly.
(function () {
    'use strict';

    if (!window.MachineCapabilities) return;

    const gated = Array.from(document.querySelectorAll('[data-needs-command]'));
    if (gated.length === 0) return;

    const bar = document.getElementById('machine-tools');
    const note = document.getElementById('machine-tools-note');

    window.MachineCapabilities.ready().then(() => {
        let hiddenInBar = 0;
        gated.forEach((node) => {
            if (window.MachineCapabilities.can(node.dataset.needsCommand)) return;
            node.hidden = true;
            if (bar && bar.contains(node)) hiddenInBar += 1;
        });
        if (!hiddenInBar || !note) return;

        // A switch of literal keys rather than one built from the platform slug:
        // tests/test_i18n.py can only scan literals, so a computed key whose translation was
        // never written would ship silently and render itself.
        note.textContent = window.MachineCapabilities.platform() === 'android'
            ? t('machine.tools.hidden_android')
            : t('machine.tools.hidden');
        note.hidden = false;
    });
})();
