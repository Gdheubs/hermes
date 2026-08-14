import { translateNow } from '@/i18n'
import { type ComposerSuggestion, offerSuggestions } from '@/store/composer-suggestions'
import { $instantAccount, claimInstantAccount } from '@/store/instant-account'

/**
 * Guest-claim event provider: after the guest account's SECOND completed turn
 * in a session — let them win twice before offering anything — one pill
 * appears: "Keep this setup". The invitation lives in the place offers live
 * (the suggestion strip), not in chrome; it is session-scoped, self-limiting
 * (withdrawn the moment the account is claimed or the install stops being a
 * guest), and one click runs the whole claim.
 *
 * Deliberately NOT turn-one: the first completed turn is the product's proof
 * moment, and interrupting it with an ask reads as a signup wall relocated.
 */

const CLAIM_AFTER_TURNS = 2

const turnsBySession = new Map<string, number>()
const offered = new Set<string>()

function suggestion(): ComposerSuggestion {
  const copy = (key: string) => translateNow(`composer.guestClaim.${key}`)

  return {
    id: 'guest-claim',
    provider: 'guest',
    icon: 'key',
    label: copy('label'),
    tip: copy('tip'),
    invoke: () => claimInstantAccount(),
    workingLabel: copy('working'),
    workingTip: copy('workingTip'),
    doneLabel: copy('done'),
    doneTip: copy('doneTip')
  }
}

/** Called from the gateway stream on message.complete for a session. */
export function reportGuestTurnComplete(sessionId: null | string | undefined): void {
  if (!sessionId || $instantAccount.get().status !== 'ready') {
    return
  }

  const turns = (turnsBySession.get(sessionId) ?? 0) + 1

  turnsBySession.set(sessionId, turns)

  if (turns >= CLAIM_AFTER_TURNS && !offered.has(sessionId)) {
    offered.add(sessionId)
    offerSuggestions(sessionId, 'guest', [suggestion()])
  }
}

// The trigger condition is "unclaimed guest" — when that stops holding
// (claimed, or the mint was never ours), withdraw everywhere at once.
$instantAccount.subscribe(state => {
  if (state.status !== 'ready' && offered.size > 0) {
    for (const sessionId of offered) {
      offerSuggestions(sessionId, 'guest', [])
    }

    offered.clear()
    turnsBySession.clear()
  }
})
