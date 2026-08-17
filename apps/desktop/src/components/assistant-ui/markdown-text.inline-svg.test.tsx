import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MarkdownTextContent } from './markdown-text'

const SIMPLE_SVG = `<svg id="raw-inline-svg" width="48" height="48" viewBox="0 0 24 24">
  <path d="M13 2L6 13h5l-1 9 8-12h-5z" />
</svg>`

function renderMarkdown(text: string, isRunning = false) {
  return render(<MarkdownTextContent isRunning={isRunning} text={text} />)
}

describe('MarkdownTextContent raw SVG', () => {
  afterEach(cleanup)

  it('renders a balanced unfenced SVG through the production markdown surface', async () => {
    const { container } = renderMarkdown(`Before\n\n${SIMPLE_SVG}\n\nAfter`)

    await waitFor(() => expect(container.querySelector('svg#raw-inline-svg')).not.toBeNull())
    expect(container.textContent).toContain('Before')
    expect(container.textContent).toContain('After')
  })

  it('sanitizes active content and dangerous URLs from an unfenced SVG', async () => {
    const dangerous = `<svg id="sanitized-inline-svg" viewBox="0 0 24 24" onload="alert(1)">
  <script>alert(1)</script>
  <foreignObject><button id="foreign-button" onclick="alert(1)">Run</button></foreignObject>
  <a id="dangerous-link" href="javascript:alert(1)" xlink:href="data:text/html,boom">
    <circle id="safe-circle" cx="12" cy="12" r="10" onclick="alert(1)" />
  </a>
</svg>`

    const { container } = renderMarkdown(dangerous)

    await waitFor(() => expect(container.querySelector('svg#sanitized-inline-svg')).not.toBeNull())

    const svg = container.querySelector('svg#sanitized-inline-svg')
    const link = container.querySelector('#dangerous-link')
    const circle = container.querySelector('#safe-circle')

    expect(svg?.hasAttribute('onload')).toBe(false)
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('foreignObject')).toBeNull()
    expect(container.querySelector('#foreign-button')).toBeNull()
    expect(link?.hasAttribute('href')).toBe(false)
    expect(link?.hasAttribute('xlink:href')).toBe(false)
    expect(circle?.hasAttribute('onclick')).toBe(false)
  })

  it.each([
    ['fenced code', `\`\`\`text\n${SIMPLE_SVG}\n\`\`\``],
    [
      'blockquoted fenced code',
      `> \`\`\`html\n${SIMPLE_SVG.split('\n')
        .map(line => `> ${line}`)
        .join('\n')}\n> \`\`\``
    ],
    [
      'nested-blockquoted fenced code',
      `> > ~~~~html\n${SIMPLE_SVG.split('\n')
        .map(line => `> > ${line}`)
        .join('\n')}\n> > ~~~~`
    ],
    ['inline code', `Keep this as code: \`${SIMPLE_SVG.replaceAll('\n', ' ')}\``],
    [
      'indented code',
      SIMPLE_SVG.split('\n')
        .map(line => `    ${line}`)
        .join('\n')
    ],
    [
      'blockquoted indented code',
      SIMPLE_SVG.split('\n')
        .map(line => `>     ${line}`)
        .join('\n')
    ]
  ])('keeps SVG inside %s as code', async (_label, text) => {
    const { container } = renderMarkdown(text)

    await waitFor(() => expect(container.textContent).toContain('<svg'))
    expect(container.querySelector('svg#raw-inline-svg')).toBeNull()
  })

  it('preserves the existing fenced SVG renderer', async () => {
    const { container } = renderMarkdown(`\`\`\`svg\n${SIMPLE_SVG}\n\`\`\``)

    await waitFor(() => expect(container.querySelector('svg#raw-inline-svg')).not.toBeNull())
  })

  it('does not cross a code fence to close malformed raw SVG', async () => {
    const text = `<svg id="malformed-inline-svg" viewBox="0 0 10 10">\n<path d="M0 0h10v10z">\n\n\`\`\`text\n</svg>\n\`\`\``
    const { container } = renderMarkdown(text)

    await waitFor(() => expect(container.textContent).toContain('</svg>'))
    expect(container.querySelector('svg#malformed-inline-svg')).toBeNull()
  })

  it('does not use indented code to close malformed raw SVG', async () => {
    const text = '<svg id="malformed-indented-svg"><path d="M0 0">\n\n    </svg>'
    const { container } = renderMarkdown(text)

    await waitFor(() => expect(container.textContent).toContain('</svg>'))
    expect(container.querySelector('svg#malformed-indented-svg')).toBeNull()
  })

  it('keeps arbitrary raw HTML inert while enabling only SVG', async () => {
    const { container } = renderMarkdown(
      `<button id="raw-html-button" onclick="alert(1)">Unsafe</button>\n\n${SIMPLE_SVG}`
    )

    await waitFor(() => expect(container.querySelector('svg#raw-inline-svg')).not.toBeNull())
    expect(container.querySelector('#raw-html-button')).toBeNull()
  })

  it('waits for the closing tag while streaming and renders once balanced', async () => {
    const incomplete = SIMPLE_SVG.slice(0, SIMPLE_SVG.lastIndexOf('</svg>'))
    const view = renderMarkdown(incomplete, true)

    expect(view.container.querySelector('svg#raw-inline-svg')).toBeNull()

    view.rerender(<MarkdownTextContent isRunning text={SIMPLE_SVG} />)
    await waitFor(() => expect(view.container.querySelector('svg#raw-inline-svg')).not.toBeNull())
  })

  const resourceSvg = `<svg id="resource-policy-svg" viewBox="0 0 40 40">
  <defs>
    <linearGradient id="safe-gradient"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#000"/></linearGradient>
    <style>@import url("http://localhost/private"); .leak { fill: url(data:image/svg+xml,boom) }</style>
    <filter id="external-filter"><feImage href="file:///etc/passwd"/></filter>
  </defs>
  <rect id="safe-gradient-rect" width="20" height="20" fill="url(#safe-gradient)"/>
  <text id="safe-text" x="1" y="30">safe</text>
  <image id="remote-image" href="https://example.com/tracker.svg"/>
  <image id="localhost-image" href="http://127.0.0.1:8080/private"/>
  <use id="external-use" href="//example.com/icons.svg#secret"/>
  <path id="external-fill" d="M0 0h1" fill="url(https://example.com/paint.svg#p)"/>
  <path id="data-stroke" d="M0 1h1" stroke="url(data:image/svg+xml,boom)"/>
  <path id="external-filter-target" d="M0 2h1" filter="url(file:///etc/passwd#f)"/>
  <circle id="styled-circle" class="leak" style="background:url(http://localhost/private)" cx="30" cy="10" r="5"/>
</svg>`

  it.each([
    ['raw SVG', resourceSvg],
    ['fenced SVG', `\`\`\`svg\n${resourceSvg}\n\`\`\``]
  ])('forbids SVG resource loads from %s while retaining safe content', async (_label, markdown) => {
    const { container } = renderMarkdown(markdown)

    await waitFor(() => expect(container.querySelector('svg#resource-policy-svg')).not.toBeNull())

    expect(container.querySelector('#safe-gradient')).not.toBeNull()
    expect(container.querySelector('#safe-gradient-rect')?.getAttribute('fill')).toBe('url(#safe-gradient)')
    expect(container.querySelector('#safe-text')?.textContent).toBe('safe')
    expect(container.querySelector('style')).toBeNull()
    expect(container.querySelector('filter')).toBeNull()
    expect(container.querySelector('feImage')).toBeNull()
    expect(container.querySelector('image')).toBeNull()
    expect(container.querySelector('use')).toBeNull()
    expect(container.querySelector('#external-fill')?.hasAttribute('fill')).toBe(false)
    expect(container.querySelector('#data-stroke')?.hasAttribute('stroke')).toBe(false)
    expect(container.querySelector('#external-filter-target')?.hasAttribute('filter')).toBe(false)
    expect(container.querySelector('#styled-circle')?.hasAttribute('style')).toBe(false)
  })
})
