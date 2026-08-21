#ifndef RUNNER_SINGLE_INSTANCE_H_
#define RUNNER_SINGLE_INSTANCE_H_

#include <windows.h>

// One FleetHub client per logon session.
//
// The client's resting state is hidden, not closed: the window's X hides it to the tray
// so polling continues, because an app that is not running cannot raise the alert that is
// the whole reason it exists (see lib/main.dart). That makes "the operator double-clicks
// the exe again" the ordinary path rather than an edge case, and without a guard it buys
// them a second process, a second tray icon, and -- the part that actually costs
// something -- a second alert poller with its own delta seen-set, so every new alert
// toasts twice.
//
// This lives in the runner rather than in Dart so the losing process can exit before the
// engine starts. A Dart-side check runs after the window exists, which the user sees.

// Takes the single-instance mutex. Returns false when another instance in this logon
// session already holds it, in which case the caller should signal that instance and
// exit. The handle is held for the lifetime of the process and deliberately never
// closed -- process exit releases it, including on a crash.
bool ClaimSingleInstance();

// The registered window message the running instance is woken with. Registered ids are
// >= 0xC000 and unique machine-wide, so no other application can collide with it and
// Flutter's own window proc will not claim it.
UINT ActivateMessage();

// Called by the instance that lost the mutex, just before it exits.
void ActivateExistingInstance();

// Called by the instance that holds the mutex when the message arrives: un-hide,
// un-minimise, and take the foreground -- the same end state as the tray menu's
// "Open FleetHub".
void ActivateThisInstance(HWND window);

#endif  // RUNNER_SINGLE_INSTANCE_H_
