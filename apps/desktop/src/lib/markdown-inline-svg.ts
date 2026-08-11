interface Range {
  end: number
  kind: 'fence' | 'indented' | 'inline'
  start: number
}

interface FenceMarker {
  blockquoteDepth: number
  character: '`' | '~'
  length: number
}

interface MarkupToken {
  end: number
  kind: 'svg-close' | 'svg-open' | 'other'
  selfClosing: boolean
}

function lineEnd(text: string, start: number): number {
  const end = text.indexOf('\n', start)

  return end === -1 ? text.length : end
}

interface ContainerLine {
  blockquoteDepth: number
  content: string
}

function stripBlockquotePrefix(line: string): ContainerLine {
  let blockquoteDepth = 0
  let cursor = 0

  while (cursor < line.length) {
    const match = /^ {0,3}>[ \t]?/.exec(line.slice(cursor))

    if (!match) {
      break
    }

    blockquoteDepth += 1
    cursor += match[0].length
  }

  return { blockquoteDepth, content: line.slice(cursor) }
}

function fenceMarker(line: string): FenceMarker | null {
  const container = stripBlockquotePrefix(line)
  const match = /^ {0,3}(`{3,}|~{3,})/.exec(container.content)

  if (!match) {
    return null
  }

  const marker = match[1] || ''

  return {
    blockquoteDepth: container.blockquoteDepth,
    character: marker[0] as FenceMarker['character'],
    length: marker.length
  }
}

function isFenceClose(line: string, marker: FenceMarker): boolean {
  const container = stripBlockquotePrefix(line)

  if (container.blockquoteDepth !== marker.blockquoteDepth) {
    return false
  }

  const content = container.content
  let cursor = 0

  while (cursor < Math.min(3, content.length) && content[cursor] === ' ') {
    cursor += 1
  }

  let run = 0

  while (content[cursor + run] === marker.character) {
    run += 1
  }

  return run >= marker.length && content.slice(cursor + run).trim() === ''
}

function collectBlockCodeRanges(text: string): Range[] {
  const ranges: Range[] = []
  let cursor = 0
  let openFence: { marker: FenceMarker; start: number } | null = null

  while (cursor < text.length) {
    const end = lineEnd(text, cursor)
    const line = text.slice(cursor, end)
    const next = end < text.length ? end + 1 : end

    if (openFence) {
      if (isFenceClose(line, openFence.marker)) {
        ranges.push({ end: next, kind: 'fence', start: openFence.start })
        openFence = null
      }
    } else {
      const marker = fenceMarker(line)

      if (marker) {
        openFence = { marker, start: cursor }
      } else {
        const content = stripBlockquotePrefix(line).content

        if (/^(?: {4}|\t)/.test(content) && content.trim()) {
          ranges.push({ end: next, kind: 'indented', start: cursor })
        }
      }
    }

    cursor = next
  }

  if (openFence) {
    ranges.push({ end: text.length, kind: 'fence', start: openFence.start })
  }

  return ranges
}

function backtickRun(text: string, start: number): number {
  let length = 0

  while (text[start + length] === '`') {
    length += 1
  }

  return length
}

function findClosingBacktickRun(text: string, start: number, length: number): number {
  let cursor = start

  while (cursor < text.length) {
    const next = text.indexOf('`', cursor)

    if (next === -1) {
      return -1
    }

    const run = backtickRun(text, next)

    if (run === length) {
      return next + run
    }

    cursor = next + run
  }

  return -1
}

function rangeContaining(ranges: Range[], offset: number): Range | null {
  let low = 0
  let high = ranges.length - 1
  let candidate: Range | null = null

  while (low <= high) {
    const middle = Math.floor((low + high) / 2)
    const range = ranges[middle]

    if (!range || range.start > offset) {
      high = middle - 1
    } else {
      candidate = range
      low = middle + 1
    }
  }

  return candidate && offset < candidate.end ? candidate : null
}

function collectProtectedRanges(text: string): Range[] {
  const blockRanges = collectBlockCodeRanges(text).sort((a, b) => a.start - b.start)
  const inlineRanges: Range[] = []
  let cursor = 0

  while (cursor < text.length) {
    const protectedRange = rangeContaining(blockRanges, cursor)

    if (protectedRange) {
      cursor = protectedRange.end

      continue
    }

    if (text[cursor] !== '`') {
      cursor += 1

      continue
    }

    const length = backtickRun(text, cursor)
    const end = findClosingBacktickRun(text, cursor + length, length)

    if (end === -1) {
      cursor += length

      continue
    }

    inlineRanges.push({ end, kind: 'inline', start: cursor })
    cursor = end
  }

  return [...blockRanges, ...inlineRanges].sort((a, b) => a.start - b.start)
}

function isEscaped(text: string, offset: number): boolean {
  let slashes = 0

  for (let cursor = offset - 1; cursor >= 0 && text[cursor] === '\\'; cursor -= 1) {
    slashes += 1
  }

  return slashes % 2 === 1
}

function findQuotedTagEnd(text: string, start: number): number {
  let quote = ''

  for (let cursor = start + 1; cursor < text.length; cursor += 1) {
    const character = text[cursor] || ''

    if (quote) {
      if (character === quote) {
        quote = ''
      }

      continue
    }

    if (character === '"' || character === "'") {
      quote = character
    } else if (character === '>') {
      return cursor + 1
    }
  }

  return -1
}

function readMarkupToken(text: string, start: number): MarkupToken | null {
  if (text.startsWith('<!--', start)) {
    const close = text.indexOf('-->', start + 4)

    return close === -1 ? null : { end: close + 3, kind: 'other', selfClosing: false }
  }

  if (text.startsWith('<![CDATA[', start)) {
    const close = text.indexOf(']]>', start + 9)

    return close === -1 ? null : { end: close + 3, kind: 'other', selfClosing: false }
  }

  if (text.startsWith('<?', start)) {
    const close = text.indexOf('?>', start + 2)

    return close === -1 ? null : { end: close + 2, kind: 'other', selfClosing: false }
  }

  const end = findQuotedTagEnd(text, start)

  if (end === -1) {
    return null
  }

  const tag = text.slice(start, end)
  const closing = /^<\s*\/\s*svg\s*>$/i.test(tag)
  const opening = !closing && /^<\s*svg(?=[\s/>])/i.test(tag)

  return {
    end,
    kind: closing ? 'svg-close' : opening ? 'svg-open' : 'other',
    selfClosing: /\/\s*>$/.test(tag)
  }
}

function createBlankLineDetector(text: string): (start: number, end: number) => boolean {
  const blankLineStarts: number[] = []
  const blankLinePattern = /\r?\n[\t ]*\r?\n/g
  let match: RegExpExecArray | null

  while ((match = blankLinePattern.exec(text)) !== null) {
    blankLineStarts.push(match.index)
  }

  let nextBlankLine = 0

  // SVG scanning asks about monotonically increasing end offsets. Advancing one
  // shared pointer keeps every blank-line boundary query O(1) amortized.
  return (start: number, end: number): boolean => {
    while (blankLineStarts[nextBlankLine] !== undefined && blankLineStarts[nextBlankLine] < end) {
      nextBlankLine += 1
    }

    return nextBlankLine > 0 && (blankLineStarts[nextBlankLine - 1] ?? -1) >= start
  }
}

function separatorBefore(value: string): string {
  if (!value || value.endsWith('\n\n')) {
    return ''
  }

  return value.endsWith('\n') ? '\n' : '\n\n'
}

function separatorAfter(text: string, offset: number): string {
  if (offset >= text.length || text.startsWith('\n\n', offset)) {
    return ''
  }

  return text[offset] === '\n' ? '\n' : '\n\n'
}

function fenceFor(svg: string): string | null {
  if (!svg.includes('```')) {
    return '```'
  }

  if (!svg.includes('~~~')) {
    return '~~~'
  }

  return null
}

/**
 * Lift balanced, bare SVG markup into the existing fenced-SVG renderer.
 *
 * Code ranges are discovered before SVG scanning, so a malformed opening tag
 * can never consume a closing tag from fenced, inline, or indented code. The
 * scanner is quote-aware and balances nested SVG tags; malformed markup stays
 * on Streamdown's inert raw-HTML path.
 */
export function fenceRawSvgBlocks(text: string): string {
  const protectedRanges = collectProtectedRanges(text)
  const crossesBlankLine = createBlankLineDetector(text)
  let activeSvg: { depth: number; lastTokenEnd: number; start: number } | null = null
  let output = ''
  let copiedThrough = 0
  let cursor = 0
  let protectedIndex = 0

  while (cursor < text.length) {
    while (protectedRanges[protectedIndex] && protectedRanges[protectedIndex].end <= cursor) {
      protectedIndex += 1
    }

    const next = text.indexOf('<', cursor)

    if (next === -1) {
      break
    }

    const protectedRange = protectedRanges[protectedIndex]

    if (protectedRange && protectedRange.start < next) {
      const indentedSvgContent =
        activeSvg &&
        protectedRange.kind === 'indented' &&
        !crossesBlankLine(activeSvg.lastTokenEnd, protectedRange.start)

      if (!indentedSvgContent) {
        activeSvg = null
      }

      cursor = protectedRange.end

      continue
    }

    if (protectedRange && next >= protectedRange.start && next < protectedRange.end) {
      const indentedSvgContent =
        activeSvg && protectedRange.kind === 'indented' && !crossesBlankLine(activeSvg.lastTokenEnd, next)

      if (!indentedSvgContent) {
        activeSvg = null
        cursor = protectedRange.end

        continue
      }
    }

    const token = readMarkupToken(text, next)

    if (!token) {
      activeSvg = null
      cursor = next + 1

      continue
    }

    if (token.kind === 'svg-open' && !token.selfClosing) {
      if (activeSvg) {
        activeSvg.depth += 1
        activeSvg.lastTokenEnd = token.end
      } else if (!isEscaped(text, next)) {
        activeSvg = { depth: 1, lastTokenEnd: token.end, start: next }
      }
    } else if (token.kind === 'svg-close' && activeSvg) {
      activeSvg.depth -= 1
      activeSvg.lastTokenEnd = token.end

      if (activeSvg.depth === 0) {
        const svg = text.slice(activeSvg.start, token.end)
        const fence = fenceFor(svg)

        if (fence) {
          const before = text.slice(copiedThrough, activeSvg.start)

          output += before
          output += separatorBefore(output)
          output += `${fence}svg\n${svg}\n${fence}`
          output += separatorAfter(text, token.end)
          copiedThrough = token.end
        }

        activeSvg = null
      }
    } else if (activeSvg) {
      activeSvg.lastTokenEnd = token.end
    }

    cursor = token.end
  }

  return copiedThrough === 0 ? text : output + text.slice(copiedThrough)
}
