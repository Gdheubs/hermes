import type { DesktopUpdateStatus, DesktopVersionInfo } from '@/global'

export const HERMES_NEW_ISSUE_URL = 'https://github.com/NousResearch/hermes-agent/issues/new'

interface DesktopProblemReportContext {
  status?: DesktopUpdateStatus | null
  version?: DesktopVersionInfo | null
}

function cleanDiagnostic(value: string | undefined): string | null {
  const normalized = value?.trim().replace(/\s+/g, ' ')

  return normalized ? normalized : null
}

function cleanRevision(value: string | undefined): string | null {
  const normalized = cleanDiagnostic(value)

  return normalized && /^[0-9a-f]{7,40}$/i.test(normalized) ? normalized.slice(0, 12) : null
}

/**
 * Build a user-reviewed GitHub issue draft from a deliberately small allowlist
 * of non-sensitive diagnostics already shown on the About page. Never include
 * logs, local paths, profile names, connection URLs, config, or transcript text.
 */
export function desktopProblemReportUrl({ status, version }: DesktopProblemReportContext): string {
  const diagnostics = [
    ['Desktop version', cleanDiagnostic(version?.appVersion)],
    ['Platform', cleanDiagnostic(version?.platform)],
    ['Electron', cleanDiagnostic(version?.electronVersion)],
    ['Hermes revision', cleanRevision(status?.currentSha)]
  ] as const

  const diagnosticLines = diagnostics.flatMap(([label, value]) => (value ? [`- ${label}: ${value}`] : []))

  const lines = [
    '## What happened?',
    '',
    '<!-- Describe the problem. -->',
    '',
    '## What did you expect?',
    '',
    '<!-- Describe what should have happened instead. -->',
    '',
    '## Steps to reproduce',
    '',
    '1. ',
    '',
    '## Screenshots (optional)',
    '',
    '<!-- Drag screenshots here if they help explain the problem. -->',
    '',
    '## Desktop diagnostics (automatically added)',
    '',
    ...diagnosticLines,
    '',
    '> Review this draft before submitting. Nothing has been sent automatically.'
  ]

  const url = new URL(HERMES_NEW_ISSUE_URL)
  url.searchParams.set('title', '[Desktop Bug]: ')
  url.searchParams.set('body', lines.join('\n'))

  return url.toString()
}
