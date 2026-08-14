import { type CSSProperties, useEffect, useRef, useState } from 'react'

import { requestComposerFocus, requestComposerInsert } from '@/app/chat/composer/focus'
import { TextTab } from '@/components/ui/text-tab'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

/**
 * Empty-thread starters — category tabs + starter prompt rows on one rail,
 * rendered with our primitives (TextTab, row-hover wash).
 *
 * The block also runs a quiet ATTRACT LOOP: on a timer, a ghost highlight
 * wanders the surface as if something were reading it — dwelling on a row,
 * moving to another, occasionally flipping to a different tab and settling
 * there. It never touches the composer and never types; it only "considers".
 * The choreography is deliberately human: variable dwell times, a pause
 * after a tab flip (reading the new list), a preference for the row after
 * the current one with occasional jumps.
 *
 * Contract, same spirit as everything on this surface:
 * - Your pointer entering the block PAUSES the ghost (it yields the room);
 *   leaving resumes it.
 * - Taking manual control — clicking a tab, pressing, focusing — SUSPENDS
 *   autoplay; it quietly resumes from wherever you left it after ~12s of
 *   being left alone.
 * - Picking a prompt (the real goal) kills it for good.
 * - prefers-reduced-motion never starts it.
 *
 * Prompts deliberately diverge from t3's chat trivia: each names something a
 * plain chat box can't do — files, terminal, browsing, schedules. Clicking
 * inserts into the composer to edit; it never sends.
 */

export function IntroStarters() {
  const { t } = useI18n()
  const copy = t.composer.starters
  const [active, setActive] = useState(0)
  // Content actually rendered in the rows block. Trails `active` by one exit
  // animation: tabs respond instantly, the page turns underneath.
  const [pageTab, setPageTab] = useState(0)
  // Which way the page came from: 1 = moved right in the tab row (new list
  // slides in from the right), -1 = moved left. Drives --intro-page-from.
  const [pageDir, setPageDir] = useState(1)
  // Two-phase page turn: 'out' plays the exit on the CURRENT list first;
  // content only swaps when that animation completes (animationend), then
  // 'in' brings the new list from the opposite side. Never a hard flash.
  const [pagePhase, setPagePhase] = useState<'in' | 'out'>('in')
  const pendingTabRef = useRef<number | null>(null)
  // The ghost's current perch: a row index in the active tab, or null while
  // it's "between thoughts" (right after a tab flip).
  const [ghostRow, setGhostRow] = useState<number | null>(null)
  // Imperative loop flags — plain mutable state shared between the effect's
  // timer chain and the pointer handlers. Not derived from anything reactive.
  // suspendUntil: epoch ms before which autoplay stays quiet (manual control);
  // manualTab: where the user steered, so the ghost resumes from THERE.
  const loopRef = useRef({ manualTab: null as number | null, paused: false, stopped: false, suspendUntil: 0 })
  // Latest-ref for the page-turn entrypoint so the one-shot ghost effect can
  // route its tab flips through the same two-phase turn as user clicks.
  const turnPageRef = useRef<(to: number) => void>(() => undefined)

  const category = copy.categories[pageTab]

  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      return
    }

    const state = loopRef.current
    state.paused = false
    state.stopped = false

    // Seeded LCG: organic-feeling but cheap, no Math.random in the loop.
    let seed = (Date.now() % 2147483646) + 1

    const rand = () => {
      seed = (seed * 48271) % 2147483647

      return seed / 2147483647
    }

    // Center-weighted noise (average of two rolls): delays cluster around
    // the middle instead of spreading flat — flat randomness reads robotic,
    // clustered-with-outliers reads human.
    const jitter = (range: number) => ((rand() + rand()) / 2) * range

    const tabCount = copy.categories.length
    let tab = 0
    let row = -1
    let sinceFlip = 0
    let timer = 0

    const later = (fn: () => void, ms: number) => {
      timer = window.setTimeout(() => {
        if (!state.stopped) {
          fn()
        }
      }, ms)
    }

    const step = () => {
      // Manual control: stay quiet until the suspension lapses, then pick up
      // FROM THE TAB THE USER CHOSE — resuming by yanking them elsewhere
      // would undo their steering.
      if (state.suspendUntil > Date.now()) {
        later(step, 1000)

        return
      }

      if (state.manualTab !== null) {
        tab = state.manualTab
        state.manualTab = null
        row = -1
        sinceFlip = 0
      }

      if (state.paused) {
        later(step, 900)

        return
      }

      // Attention isn't continuous: sometimes the ghost just stops looking
      // at anything for a while — hand off the highlight, sit blank, resume.
      // More likely the longer it's been actively reading.
      if (row >= 0 && rand() < 0.3) {
        row = -1
        setGhostRow(null)
        later(step, 2200 + jitter(2600))

        return
      }

      // Flip tabs more eagerly the longer it's lingered on one — like a
      // reader who's finished a list — never twice in a row.
      const flipTab = sinceFlip >= 2 && rand() < 0.22 + sinceFlip * 0.1

      if (flipTab) {
        tab = (tab + 1 + Math.floor(rand() * (tabCount - 1))) % tabCount
        sinceFlip = 0
        row = -1
        turnPageRef.current(tab)
        setGhostRow(null)

        // Beat of "taking in the new list" before settling on a row —
        // sometimes it's a skim, sometimes a real read.
        later(step, 1400 + jitter(1800))

        return
      }

      const rowCount = copy.categories[tab]?.prompts.length ?? 0

      if (rowCount === 0) {
        later(step, 1200)

        return
      }

      // Mostly read downward to the next row; sometimes the eye jumps.
      const next =
        row >= 0 && rand() < 0.62 ? (row + 1) % rowCount : (row + 1 + Math.floor(rand() * rowCount)) % rowCount

      row = next
      sinceFlip += 1
      setGhostRow(next)
      // Dwell like reading, not scanning: a fat middle with a long tail —
      // most rows get a real look, some get genuinely studied.
      later(step, 2000 + jitter(2400) + (rand() < 0.22 ? 2200 : 0))
    }

    // Let the surface land before anything stirs.
    later(step, 3200 + jitter(1200))

    return () => {
      state.stopped = true
      window.clearTimeout(timer)
    }
  }, [copy.categories])

  if (!category) {
    return null
  }

  const stopLoop = () => {
    loopRef.current.stopped = true
    setGhostRow(null)
  }

  // Manual control: don't kill autoplay, suspend it — the ghost yields for
  // ~12s (refreshed by every further interaction) and then resumes from the
  // user's tab. Browsing the tabs yourself shouldn't permanently silence
  // the surface; committing to a prompt should.
  const suspendLoop = (manualTab?: number) => {
    loopRef.current.suspendUntil = Date.now() + 12000

    if (manualTab !== undefined) {
      loopRef.current.manualTab = manualTab
    }

    setGhostRow(null)
  }

  // The one page-turn entrypoint (user clicks and ghost flips both land
  // here): the tab highlight moves IMMEDIATELY (input must read instant),
  // the direction is set, and the exit starts. The list content itself only
  // swaps in onAnimationEnd — the old rows are fully out before the new
  // ones exist. Re-steering mid-exit just retargets the pending tab.
  const turnPage = (to: number) => {
    if (to === active) {
      return
    }

    // Reduced motion: no exit animation will run, so no animationend will
    // arrive — swap immediately instead of waiting forever.
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      setActive(to)
      setPageTab(to)

      return
    }

    pendingTabRef.current = to
    setPageDir(to > active ? 1 : -1)
    setActive(to)
    setPagePhase('out')
  }

  const finishTurn = () => {
    if (pagePhase !== 'out') {
      return
    }

    const to = pendingTabRef.current

    if (to !== null) {
      pendingTabRef.current = null
      setPageTab(to)
    }

    setPagePhase('in')
  }

  turnPageRef.current = turnPage

  const pick = (prompt: string) => {
    stopLoop()
    requestComposerInsert(prompt, { mode: 'block', target: 'main' })
    requestComposerFocus('main')
  }

  return (
    <div
      className="pointer-events-auto flex w-full flex-col gap-3"
      onFocusCapture={() => suspendLoop()}
      onPointerDownCapture={() => suspendLoop()}
      onPointerEnter={() => {
        loopRef.current.paused = true
        setGhostRow(null)
      }}
      onPointerLeave={() => {
        loopRef.current.paused = false
      }}
    >
      <div className="flex flex-wrap items-center gap-4">
        {copy.categories.map((entry, index) => (
          <TextTab
            active={index === active}
            className="px-0"
            key={entry.label}
            onClick={() => {
              suspendLoop(index)
              turnPage(index)
            }}
          >
            {entry.label}
          </TextTab>
        ))}
      </div>

      {/* Row hover pads 12px inward, so pull the block 12px outward: the row
          TEXT sits on the same rail as the tabs and title above, and the
          hover wash hangs into the gutter like the sidebar's rows do.
          Two-phase page turn: exit eases the whole old list out toward the
          side you're leaving; onAnimationEnd swaps content (key), then each
          row slides in individually, staggered by sibling-index(). */}
      <div
        className={cn(
          '-mx-3 flex flex-col motion-reduce:*:animate-none motion-reduce:animate-none',
          pagePhase === 'out' ? 'intro-page-out' : 'intro-page-stagger'
        )}
        key={pageTab}
        onAnimationEnd={finishTurn}
        style={{ '--intro-page-from': pageDir > 0 ? '8px' : '-8px' } as CSSProperties}
      >
        {category.prompts.map((prompt, index) => (
          <button
            className={cn(
              'rounded-2xl px-3 py-2 text-left text-[0.9375rem] text-muted-foreground',
              // Transition only when NOT hovered: your own hover paints
              // instantly (real input reads as immediate), while the wash
              // fading off — and the ghost gliding row to row — stays eased.
              'not-hover:transition-[background-color,color] not-hover:duration-700 not-hover:ease-out',
              'hover:bg-(--ui-row-hover-background) hover:text-foreground',
              ghostRow === index && 'bg-(--ui-row-hover-background) text-foreground'
            )}
            key={prompt}
            onClick={() => pick(prompt)}
            type="button"
          >
            {prompt}
          </button>
        ))}
      </div>
    </div>
  )
}
