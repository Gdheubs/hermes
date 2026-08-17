# Native Web Fetch Provider for Hermes Agent

Local HTTP fetch + trafilatura extract provider for Hermes Agent.  
**No API key required.** Register as `web.extract_backend: native`.

## Usage

```yaml
# config.yaml
web:
  search_backend: ddgs       # or searxng, brave-free, etc.
  extract_backend: native    # local HTTP extraction, no API key

plugins:
  enabled:
    - web/native
```

### Zero-config

Explicit config is optional. On an install with **no web API keys and nothing
configured**, backend selection now auto-resolves to the free stack:

- **search** → `ddgs` (when the `ddgs` package is importable)
- **extract** → `native` (when this plugin is installed)

A deliberately configured paid backend always wins — the free providers are
only chosen as the fallback when nothing else can serve the capability.

Install dependencies:

```bash
pip install "hermes-agent[native-fetch]"
# or manually:
uv pip install "trafilatura>=2.0,<3" "html2text>=2025.4.15,<2026"
```

## How It Works

1. Receives a URL to extract
2. Fetches the page via `httpx` (HTTP GET)
3. Extracts main content via `trafilatura` (markdown output, headings preserved)
4. Collapses duplicated in-page anchor links (e.g. `## [Q1](url#q1)Q1` → `## Q1`)
5. Falls back to raw HTML + `html2text` when trafilatura finds no main content
6. Returns structured result to the agent

Extract-only — pair with any search provider (`ddgs`, `searxng`, etc.).

Set it via `web.extract_backend`, **not** `web.backend`: the latter is the
shared fallback for both capabilities, so pointing it at this plugin leaves
`web_search` with a backend that cannot search (the tool then returns an
explicit "extract-only backend" error).

## Configuration

All behavioral settings live under `web.native` in `config.yaml` (defaults
shown).

```yaml
web:
  extract_backend: native
  extract_char_limit: 15000      # per-page budget sent to the model (all backends)
  native:
    timeout: 30                  # HTTP request timeout (seconds)
    max_redirects: 5             # Max redirects to follow (each hop SSRF-checked)
    max_response_bytes: 2000000  # Max bytes read off the socket (2 MB)
    cache_ttl: 900               # In-memory cache TTL (seconds, 15 min); 0 disables
    trafilatura: true            # Use trafilatura main-content extraction
    favor_precision: false       # trafilatura: prefer precision over recall
    include_links: true          # trafilatura: keep hyperlinks in markdown
    trust_env: false            # Trust env for BOTH proxy + TLS (see below)
    user_agent: ""               # Override User-Agent; empty → provider default
```

### Environment trust (`trust_env`)

Maps 1:1 onto httpx's own `trust_env` flag, which is the authority for **two**
environment-sourced behaviours at once — not just proxies:

1. **Proxy pickup** — when `true`, honours `HTTP_PROXY` / `HTTPS_PROXY` /
   `ALL_PROXY` from the environment. When `false`, httpx is told to ignore
   ambient proxy variables entirely (this is what actually switches off an
   ambient proxy — `proxy: None` alone would leave httpx's default `True`
   and silently keep routing through the environment).
2. **TLS trust store from env** — when `true` **and** `verify` is the default
   (`True`), the system `SSL_CERT_FILE` / `SSL_CERT_DIR` are consulted;
   when `false`, HTTPS verification always falls back to the bundled
   **certifi** bundle.

So `trust_env: false` affects **every** HTTPS fetch, proxied or not: a CA you
installed system-wide via `SSL_CERT_FILE` will be ignored, and any host signed
by it will fail verification. Set it to `true` if you rely on a local/self-
signed/corporate CA bundle from the environment.

### Size limits

Two different budgets, at two different layers:

- `web.native.max_response_bytes` — how much is read **off the socket**.
  Enforced while streaming: the `Content-Length` header is checked before the
  body is touched, and the read stops mid-stream once the budget is spent, so
  a chunked response with no declared length is bounded too.
- `web.extract_char_limit` — how many **characters reach the model**. Owned by
  `web_extract_tool` and applied identically to every backend; oversized pages
  are head+tail truncated with a footer and the full text is stored under
  `cache/web`.

The provider deliberately has no character knob of its own: it returns
`content == raw_content` like every other provider and lets the tool budget it.

### Caching

Extracted pages are cached in memory, per process, keyed by URL *and* render
mode (`markdown` / `text`), so the two renderings never serve each other's
output. `cache_ttl: 0` turns caching off entirely — no reads, no writes.

Expiry is lazy: there is no background reaper. A lapsed entry stops being
served the moment its TTL passes, and is freed when its key is read again or
on the next fetch of any URL (success or failure). Only a process that fetches
nothing further keeps lapsed bodies resident, and those stay under the caps
below.

Two caps bound the cache, because a page count says little about memory:

- **512 entries**
- **64 MB of extracted text in total**

When either cap is reached, the **oldest write** is evicted — the caps never
wait for a TTL to lapse. Eviction is by write time, not access time: reading
an entry does not refresh it, so a frequently read page written long ago is
evicted before a cold one written recently. The entry just written is never
the one evicted, and a single page larger than the whole budget is not cached
at all.

`trafilatura: false` disables main-content extraction entirely — the page is
returned as raw HTML (script/style stripped) converted via `html2text`,
including site navigation.

## Dependencies

- `trafilatura` — main-content extraction (markdown, headings preserved)
- `html2text` — HTML to Markdown fallback conversion
- `httpx` — Async HTTP client (core Hermes dependency)

## Security

- SSRF protection via `async_is_safe_url` — blocks requests to private/internal networks
- Redirects are followed manually and **every hop is re-validated** with
  `async_is_safe_url` before the next request, so a public URL cannot redirect
  into a private/internal address
- URL secrets detection (`_PREFIX_RE`) runs in `web_extract_tool` before dispatch
  — blocks URLs containing API keys/tokens
- No JavaScript execution — static HTML only
