import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { FileDiffPanel } from './diff-lines'

// One add hunk over two context lines. No `path` is passed so the panel takes
// the plain (non-Shiki) path — no dynamic import, deterministic DOM.
const DIFF = `diff --git a/notes.txt b/notes.txt
index 0000001..0000002 100644
--- a/notes.txt
+++ b/notes.txt
@@ -1,2 +1,3 @@
 keep this line
+add a line that is long enough to wrap
`

describe('FileDiffPanel wrap', () => {
  it('renders lines non-wrapping by default', () => {
    render(<FileDiffPanel diff={DIFF} />)

    const added = screen.getByText('add a line that is long enough to wrap')

    expect(added.className).toContain('whitespace-pre')
    expect(added.className).not.toContain('whitespace-pre-wrap')
  })

  it('swaps lines to the wrapping classes when wrap is on', () => {
    render(<FileDiffPanel diff={DIFF} wrap />)

    const added = screen.getByText('add a line that is long enough to wrap')
    const context = screen.getByText('keep this line')

    expect(added.className).toContain('whitespace-pre-wrap')
    expect(added.className).toContain('break-words')
    expect(added.className.split(/\s+/)).not.toContain('whitespace-pre')
    expect(context.className).toContain('whitespace-pre-wrap')
  })

  it('keeps the wrap classes in the windowed shell used by the review pane', () => {
    render(<FileDiffPanel diff={DIFF} virtualized wrap />)

    const added = screen.getByText('add a line that is long enough to wrap')

    expect(added.className).toContain('whitespace-pre-wrap')
    expect(added.className.split(/\s+/)).not.toContain('whitespace-pre')
  })
})
