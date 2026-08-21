#include "single_instance.h"

namespace {

// Local\, not Global\. The scope that is right here is the logon session: two operators
// on the same box over RDP should each get a client, and each is signed in to the hub as
// themselves with their own device token. A Global\ mutex would hand the first one an app
// and the second one a process that exits without saying why.
constexpr const wchar_t kMutexName[] = L"Local\\FleetHubClient.SingleInstance";

constexpr const wchar_t kActivateMessageName[] = L"FleetHubClient.Activate";

}  // namespace

bool ClaimSingleInstance() {
  // Held for the life of the process on purpose; see the header.
  HANDLE mutex = ::CreateMutexW(nullptr, TRUE, kMutexName);
  if (mutex == nullptr) {
    // Nothing sensible to do about a mutex we could not create, and refusing to start
    // over it would be a worse failure than the duplicate instance it guards against.
    return true;
  }
  return ::GetLastError() != ERROR_ALREADY_EXISTS;
}

UINT ActivateMessage() {
  static const UINT message = ::RegisterWindowMessageW(kActivateMessageName);
  return message;
}

void ActivateExistingInstance() {
  // Windows will not let a background process take the foreground on its own. This is the
  // sanctioned handover: we are the process the user just launched, so we hold the
  // foreground right, and we pass it on. Without this the running instance comes back
  // behind whatever is on top and only flashes its taskbar button -- which, when it was
  // hidden in the tray, means the operator sees nothing happen at all.
  ::AllowSetForegroundWindow(ASFW_ANY);

  // Broadcast rather than a located HWND: a broadcast reaches hidden top-level windows,
  // and hidden is exactly the state the running instance is usually in. It also means
  // nothing here has to identify the other process's window, which the stock Flutter
  // class name (FLUTTER_RUNNER_WIN32_WINDOW, shared by every Flutter app on the machine)
  // is a poor basis for.
  //
  // If a future custom URL scheme needs the second launch's command line forwarded, that
  // is where this grows a window lookup: WM_COPYDATA cannot be broadcast.
  ::PostMessageW(HWND_BROADCAST, ActivateMessage(), 0, 0);
}

void ActivateThisInstance(HWND window) {
  if (window == nullptr) {
    return;
  }
  ::ShowWindow(window, SW_SHOW);
  if (::IsIconic(window)) {
    ::ShowWindow(window, SW_RESTORE);
  }
  ::SetForegroundWindow(window);
}
