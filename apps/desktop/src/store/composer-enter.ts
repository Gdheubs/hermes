import { Codecs, persistentAtom } from '@/lib/persisted'

const COMPOSER_ENTER_SENDS_STORAGE_KEY = 'hermes.desktop.composerEnterSends'

// What a bare Enter does in the chat composer. On (the default) a plain Enter
// sends the draft and Shift+Enter inserts a newline; off flips the two roles
// so Enter inserts a newline and Shift+Enter sends — a common chat-app toggle.
// This is a renderer-local presentation pref for this window's chat surface,
// per-window-machine: it rides the same persistentAtom machinery as the other
// desktop prefs (statusbar, zoom, …), never crosses the gateway, and is not
// backend config. Defaulting ON preserves today's behavior for existing users.
export const $composerEnterSends = persistentAtom(COMPOSER_ENTER_SENDS_STORAGE_KEY, true, Codecs.bool)

export function setComposerEnterSends(sends: boolean) {
  $composerEnterSends.set(sends)
}

export function toggleComposerEnterSends() {
  $composerEnterSends.set(!$composerEnterSends.get())
}
