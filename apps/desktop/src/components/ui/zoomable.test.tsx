import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Zoomable } from './zoomable'

afterEach(cleanup)

// Mermaid 11.16 emits `width="100%"` on the root svg; the fixed embed normalizes
// it (normalizeSvgSize) so the overlay has a real intrinsic size.
const MERMAID_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="260.34375" height="70" class="flowchart" style="max-width: 260.34375px;" viewBox="0 0 260.34375 70" role="graphics-document document"><rect width="260.34375" height="70" fill="#eee"/></svg>`

describe('Zoomable', () => {
  it('opens the full-view overlay when the trigger is clicked', () => {
    render(
      <Zoomable
        label="Open diagram"
        overlay={<div dangerouslySetInnerHTML={{ __html: MERMAID_SVG }} data-testid="overlay" />}
      >
        <div dangerouslySetInnerHTML={{ __html: MERMAID_SVG }} data-testid="inline" />
      </Zoomable>
    )

    expect(screen.queryByTestId('overlay')).toBeNull()
    fireEvent.click(screen.getByTitle('Open diagram'))
    expect(screen.getByTestId('overlay')).toBeTruthy()
  })

  it('overlay svg carries an explicit pixel width (the blank-dialog contract)', () => {
    render(
      <Zoomable
        label="Open diagram"
        overlay={<div dangerouslySetInnerHTML={{ __html: MERMAID_SVG }} data-testid="overlay" />}
      >
        <div dangerouslySetInnerHTML={{ __html: MERMAID_SVG }} />
      </Zoomable>
    )

    fireEvent.click(screen.getByTitle('Open diagram'))
    const svg = screen.getByTestId('overlay').querySelector('svg')

    expect(svg).toBeTruthy()
    expect(svg?.getAttribute('width')).toBe('260.34375')
    expect(svg?.getAttribute('height')).toBe('70')
  })
})