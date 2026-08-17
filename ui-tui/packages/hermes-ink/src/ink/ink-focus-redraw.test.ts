import { EventEmitter } from 'events'

import React from 'react'
import { describe, expect, it } from 'vitest'

import Text from './components/Text.js'
import Ink from './ink.js'
import { ERASE_SCREEN } from './termio/csi.js'
import { DISABLE_MOUSE_TRACKING } from './termio/dec.js'

class FakeTty extends EventEmitter {
  chunks: string[] = []
  columns = 40
  rows = 8
  isTTY = true

  write(chunk: string | Uint8Array, cb?: (err?: Error | null) => void): boolean {
    this.chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    cb?.()

    return true
  }
}

type InkPrivate = {
  handleTerminalFocusChange: (isFocused: boolean) => void
}

const peek = (ink: Ink): InkPrivate => ink as unknown as InkPrivate
const tick = () => new Promise<void>(resolve => queueMicrotask(resolve))

describe('Ink focus recovery', () => {
  it('repaints without clearing the screen or re-asserting terminal modes', async () => {
    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    ink.setAltScreenActive(true, 'all')
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    await tick()
    stdout.chunks = []

    peek(ink).handleTerminalFocusChange(true)
    await tick()

    const output = stdout.chunks.join('')
    expect(output).toContain('hello')
    expect(output).not.toContain(ERASE_SCREEN)
    expect(output).not.toContain(DISABLE_MOUSE_TRACKING)

    ink.unmount()
  })
})
