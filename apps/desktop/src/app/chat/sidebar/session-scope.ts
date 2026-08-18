import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'
import type { SessionInfo } from '@/types/hermes'

/**
 * Resolve which sessions belong in the flat "recents" list for the current
 * profile scope.
 *
 * `profileScope` is the workspace-switcher context: a concrete profile key
 * (show only that profile's sessions) or `ALL_PROFILES` (show every profile).
 * The ALL scope must fan in *all* sessions regardless of the grouped view's
 * availability. In particular a single-profile user can still land in the
 * ALL scope (Grouping → Profile persists `$showAllProfiles`, which drives
 * `$profileScope` to `ALL_PROFILES`), while the grouped rendering is gated on
 * `multiProfile` and therefore off. Filtering against the `__all__` sentinel
 * in that case would empty the list even though the backend served every row
 * — so the ALL scope returns the whole set, never a filter match.
 */
export function selectVisibleSessions(
  sessions: SessionInfo[],
  profileScope: string
): SessionInfo[] {
  if (profileScope === ALL_PROFILES) {
    return sessions
  }

  return sessions.filter(s => normalizeProfileKey(s.profile) === profileScope)
}
