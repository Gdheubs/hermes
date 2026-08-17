import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isCmdStartUnsafeForUrl, wslWindowsOpenUrlArgs } from './wsl-open-url'

test('wslWindowsOpenUrlArgs uses rundll32 FileProtocolHandler, not cmd start', () => {
  const url = 'https://example.com/?a=1&calc&z=2'
  const launched = wslWindowsOpenUrlArgs(url)
  assert.equal(launched.command, 'rundll32.exe')
  assert.deepEqual(launched.args, ['url.dll,FileProtocolHandler', url])
  assert.notEqual(launched.command, 'cmd.exe')
})

test('ampersand query strings are unsafe for unquoted cmd start', () => {
  assert.equal(isCmdStartUnsafeForUrl('https://example.com/?a=1&calc&z=2'), true)
  assert.equal(isCmdStartUnsafeForUrl('https://example.com/path'), false)
})
