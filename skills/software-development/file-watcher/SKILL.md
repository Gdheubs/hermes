---
name: file-watcher
description: Watch filesystem changes with hermes watch polling command.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [file-system, monitoring, development, watch]
---

# File Watcher

`hermes watch` reports filesystem changes as they happen, and can re-run a
command each time something changes.

## When to Use

- Re-running tests or a linter on every save
- Watching log or output files while another process writes them
- Detecting when an external process writes or deletes files
- Confirming that a build step actually touched what you expected

## Usage

```bash
# Watch current directory
hermes watch .

# Watch several trees at once
hermes watch src/ tests/

# Only report matching files
hermes watch . --pattern "*.py,*.js"

# Skip build artifacts (matches at any depth)
hermes watch . --ignore "*.pyc,__pycache__/*,node_modules/*"

# Re-run a command after each batch of changes
hermes watch . --pattern "*.py" --command "pytest tests/ -x"

# Top level only
hermes watch . --no-recursive

# Sweep interval, in seconds (default: 1.0)
hermes watch . --interval 2.0
```

Paths are reported relative to the watched root. When several roots are
given, each path is prefixed with its root's name so `src/main.py` and
`tests/main.py` stay distinguishable.

`--pattern` and `--ignore` take comma-separated globs. A glob is matched
against the filename, the root-relative path, and that path at any depth —
so `__pycache__/*` skips the directory wherever it appears. `--ignore` wins
over `--pattern`.

## `--command`

The command runs through the shell after any sweep that produced events, not
once per file — a burst of saves triggers one run. Runs are sequential, so a
slow command delays the next sweep rather than overlapping with itself. The
exit status of the last run becomes the exit status of `hermes watch`.

## Backends

By default the watcher polls, which needs no extra packages and works
everywhere. If the optional `watchdog` package is importable, OS-level
notifications are used instead — cheaper on large trees, and `--interval`
stops applying. `watchdog` is not a Hermes dependency; install it into the
same environment if you want it:

```bash
uv pip install watchdog
```

| Platform | Backend with watchdog | Without |
|----------|-----------------------|---------|
| Linux | inotify | polling |
| macOS | FSEvents | polling |
| Windows | ReadDirectoryChangesW | polling |

## Notes

- Polling compares each file's modification time and size, so an edit that
  changes neither (rare, and only within one filesystem clock tick) is missed.
- Large trees cost one directory walk per interval; narrow the paths or raise
  `--interval` rather than watching a whole home directory.
- Ctrl+C (or SIGTERM) stops the watcher and prints the event count.

## Agent Guidance

Prefer `hermes watch` over hand-rolled `while true; do ... sleep; done` loops
in the terminal: it is consistent across platforms, filters noise with
`--pattern`/`--ignore`, and exits cleanly.

```bash
hermes watch <paths> [--pattern <glob>] [--ignore <glob>] [--command <cmd>]
```

`hermes watch` runs until stopped, so start it in the background (or in its
own terminal) when you still need the shell — a foreground call blocks until
the user interrupts it.
