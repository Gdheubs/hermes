import { shouldBlockPtyInput, type PtyConnectionState } from "./pty-reconnect";

const WS_OPEN = 1;

export type PtyKeyboardShortcut =
  | "copy"
  | "delete-word-backward"
  | "delete-word-forward"
  | "insert-newline"
  | "pass";

export function resolvePtyKeyboardShortcut(
  ev: Pick<KeyboardEvent, "altKey" | "ctrlKey" | "key" | "metaKey" | "shiftKey">,
  isMac: boolean,
  hasTerminalSelection: boolean,
): PtyKeyboardShortcut {
  const key = ev.key.toLowerCase();
  const copyPressed = isMac
    ? ev.metaKey && !ev.ctrlKey
    : ev.ctrlKey && !ev.altKey && !ev.metaKey;

  if (copyPressed && key === "c" && hasTerminalSelection) {
    return "copy";
  }

  if (
    ev.ctrlKey &&
    !ev.shiftKey &&
    !ev.altKey &&
    !ev.metaKey &&
    ev.key === "Backspace"
  ) {
    return "delete-word-backward";
  }

  if (
    ev.ctrlKey &&
    !ev.shiftKey &&
    !ev.altKey &&
    !ev.metaKey &&
    ev.key === "Delete"
  ) {
    return "delete-word-forward";
  }

  if (
    ((ev.shiftKey && !ev.altKey) || (ev.altKey && !ev.shiftKey)) &&
    !ev.ctrlKey &&
    !ev.metaKey &&
    ev.key === "Enter"
  ) {
    return "insert-newline";
  }

  return "pass";
}

// Guarded sender for shortcut escape sequences. Applies the same gate as the
// term.onData path (socket OPEN + shouldBlockPtyInput) so a reconnecting or
// closed session never receives shortcut bytes.
export function sendPtyShortcutSequence(
  ws: Pick<WebSocket, "readyState" | "send"> | null,
  ptyState: PtyConnectionState,
  sequence: string,
): boolean {
  if (!ws || ws.readyState !== WS_OPEN || shouldBlockPtyInput(ptyState)) {
    return false;
  }

  ws.send(sequence);

  return true;
}
