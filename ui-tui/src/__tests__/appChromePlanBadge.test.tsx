import React from 'react'
import { describe, expect, it } from 'vitest'

import { StatusRule } from '../components/appChrome.js'
import { DEFAULT_THEME } from '../theme.js'

type ReactNodeLike = React.ReactNode

const textContent = (node: ReactNodeLike): string => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return ''
  }

  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }

  if (Array.isArray(node)) {
    return node.map(textContent).join('')
  }

  if (React.isValidElement(node)) {
    return textContent(node.props.children)
  }

  return ''
}

const findElementWithText = (node: ReactNodeLike, needle: string): React.ReactElement | null => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElementWithText(child, needle)
      if (found) {
        return found
      }
    }
    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  const deeper = findElementWithText(node.props.children, needle)
  if (deeper) {
    return deeper
  }

  return textContent(node).includes(needle) ? node : null
}

const baseProps = {
  bgCount: 0,
  busy: false,
  cols: 100,
  cwdLabel: '~/repo',
  interactionMode: 'build' as const,
  liveSessionCount: 0,
  model: 'opus-4.8',
  sessionStartedAt: null,
  status: 'ready',
  statusColor: DEFAULT_THEME.color.ok,
  t: DEFAULT_THEME,
  turnStartedAt: null,
  usage: { context_max: 200_000, context_percent: 25, context_used: 50_000, total: 50_000 },
  voiceLabel: ''
}

describe('StatusRule interaction mode badge', () => {
  it('does not show PLAN badge in BUILD mode', () => {
    const element = StatusRule({ ...baseProps, interactionMode: 'build' })
    const rendered = textContent(element)
    expect(rendered).not.toContain('PLAN')
  })

  it('shows PLAN badge when interactionMode is plan', () => {
    const element = StatusRule({ ...baseProps, interactionMode: 'plan' })
    const rendered = textContent(element)
    expect(rendered).toContain('PLAN')
  })

  it('PLAN badge renders with bold emphasis', () => {
    const element = StatusRule({ ...baseProps, interactionMode: 'plan' })
    const planEl = findElementWithText(element, 'PLAN')
    expect(planEl).not.toBeNull()
    expect(planEl!.props.bold).toBe(true)
  })

  it('defaults to build when interactionMode is omitted', () => {
    const { interactionMode: _, ...propsWithoutMode } = baseProps
    const element = StatusRule(propsWithoutMode)
    const rendered = textContent(element)
    expect(rendered).not.toContain('PLAN')
  })

  it('PLAN badge appears alongside focus badge when both active', () => {
    const element = StatusRule({ ...baseProps, interactionMode: 'plan', focusView: true })
    const rendered = textContent(element)
    expect(rendered).toContain('PLAN')
    expect(rendered).toContain('focus')
  })
})
