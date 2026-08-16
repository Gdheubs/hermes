import { describe, expect, it } from 'vitest'

import { desktopProblemReportUrl, HERMES_NEW_ISSUE_URL } from './desktop-problem-report'

const VERSION = {
  appVersion: '0.17.0',
  electronVersion: '39.2.7',
  nodeVersion: '22.20.0',
  platform: 'darwin',
  hermesRoot: '/Users/private-name/.hermes/hermes-agent'
}

describe('desktopProblemReportUrl', () => {
  it('opens only the upstream issue composer with allowlisted diagnostics', () => {
    const report = new URL(
      desktopProblemReportUrl({
        version: VERSION,
        status: {
          supported: true,
          currentSha: '0123456789abcdef0123456789abcdef01234567'
        }
      })
    )

    expect(`${report.origin}${report.pathname}`).toBe(HERMES_NEW_ISSUE_URL)
    expect([...report.searchParams.keys()]).toEqual(['title', 'body'])
    expect(report.searchParams.get('title')).toBe('[Desktop Bug]: ')

    const body = report.searchParams.get('body') ?? ''

    expect(body).toContain('## What happened?')
    expect(body).toContain('## Screenshots (optional)')
    expect(body).toContain('## Desktop diagnostics (automatically added)')
    expect(body).toContain('- Desktop version: 0.17.0')
    expect(body).toContain('- Platform: darwin')
    expect(body).toContain('- Electron: 39.2.7')
    expect(body).toContain('- Hermes revision: 0123456789ab')
    expect(body).toContain('Nothing has been sent automatically.')
  })

  it('encodes Unicode, newlines, and reserved characters in diagnostics', () => {
    const report = new URL(
      desktopProblemReportUrl({
        version: {
          ...VERSION,
          appVersion: '版本 0.17.0 + beta&1\nchannel',
          platform: 'darwin / arm64'
        }
      })
    )

    const body = report.searchParams.get('body') ?? ''

    expect(body).toContain('- Desktop version: 版本 0.17.0 + beta&1 channel')
    expect(body).toContain('- Platform: darwin / arm64')
  })

  it('omits blank, unavailable, or malformed diagnostics without leaking local paths', () => {
    const report = new URL(
      desktopProblemReportUrl({
        version: { ...VERSION, electronVersion: '   ' },
        status: { supported: true, currentSha: 'not a revision /Users/private-name' }
      })
    )

    const body = report.searchParams.get('body') ?? ''

    expect(body).not.toContain('Hermes revision:')
    expect(body).not.toContain('Electron:')
    expect(body).not.toContain(VERSION.hermesRoot)
    expect(body).not.toContain(VERSION.nodeVersion)
    expect(body).not.toContain('private-name')
    expect(body).not.toContain('undefined')
    expect(body).not.toContain('null')
  })

  it('still produces a useful editable draft before version discovery completes', () => {
    const report = new URL(desktopProblemReportUrl({}))
    const body = report.searchParams.get('body') ?? ''

    expect(body).toContain('## Steps to reproduce')
    expect(body).not.toContain('Desktop version:')
    expect(body).not.toContain('Platform:')
    expect(body).not.toContain('Electron:')
  })
})
