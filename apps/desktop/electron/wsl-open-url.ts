/**
 * Open an http(s)/mailto URL from WSL onto the Windows host without
 * going through ``cmd.exe /c start``.
 *
 * ``cmd.exe /c start "" <url>`` is assembled unquoted by WSL binfmt
 * interop when the URL has no spaces. ``&`` in a query string then
 * becomes a command separator, so a chat link like
 * ``https://example.com/?a=1&calc&z=2`` runs ``calc`` on the host.
 *
 * ``rundll32.exe url.dll,FileProtocolHandler`` takes the URL as a
 * single argument and does not re-parse it as a cmd command line.
 */
export function wslWindowsOpenUrlArgs(url: string): { command: string; args: string[] } {
  return {
    command: 'rundll32.exe',
    args: ['url.dll,FileProtocolHandler', url]
  }
}

export function isCmdStartUnsafeForUrl(url: string): boolean {
  return /[&|^<>%]/.test(url)
}
