# ByteRover Memory Provider

Persistent memory via the `brv` CLI — hierarchical knowledge tree with tiered retrieval (fuzzy text → LLM-driven search).

## Requirements

Install the ByteRover CLI:
```bash
curl -fsSL https://byterover.dev/install.sh | sh
# or
npm install -g byterover-cli
```

## Setup

```bash
hermes memory setup    # select "byterover"
```

Or manually:
```bash
hermes config set memory.provider byterover
# Optional cloud sync:
echo "BRV_API_KEY=your-key" >> ~/.hermes/.env
```

## Config

`BRV_API_KEY` is optional and only needed for cloud sync. ByteRover is local-first by default.

ByteRover queries time out after 10 seconds unless configured otherwise:

```bash
hermes config set memory.byterover.timeout_query 30
hermes config set memory.prefetch_timeout 31
```

Both values are in seconds and accept numbers from 0.01 to 3600. The outer `memory.prefetch_timeout` must be larger than `memory.byterover.timeout_query`; otherwise Hermes may stop waiting before the ByteRover query finishes.

Working directory: `$HERMES_HOME/byterover/` (profile-scoped).

## Tools

| Tool | Description |
|------|-------------|
| `brv_query` | Search the knowledge tree |
| `brv_curate` | Store facts, decisions, patterns |
| `brv_status` | CLI version, tree stats, sync state |
