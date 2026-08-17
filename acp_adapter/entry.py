"""CLI entry point for the hermes-agent ACP adapter.

Loads environment variables from ``~/.hermes/.env``, configures logging
to write to stderr (so stdout is reserved for ACP JSON-RPC transport),
and starts the ACP agent server.

Usage::

    python -m acp_adapter.entry
    # or
    hermes acp
    # or
    hermes-acp
"""

# IMPORTANT: hermes_bootstrap must be the very first import — UTF-8 stdio
# on Windows.  No-op on POSIX.  See hermes_bootstrap.py for full rationale.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    # Graceful fallback when hermes_bootstrap isn't registered in the venv
    # yet — happens during partial ``hermes update`` where git-reset landed
    # new code but ``uv pip install -e .`` didn't finish.  Missing bootstrap
    # means UTF-8 stdio setup is skipped on Windows; POSIX is unaffected.
    pass
else:
    # Stop a ``utils/``/``proxy/``/``ui/`` package in the launch directory from
    # shadowing Hermes's own modules — ``hermes acp`` can be started from any
    # cwd, including a project that has same-named packages on its path.
    hermes_bootstrap.harden_import_path()

import argparse
import asyncio
import contextlib
import logging
import os
import stat
import sys
import threading
from pathlib import Path
from hermes_constants import get_hermes_home


# Methods clients send as periodic liveness probes. They are not part of the
# ACP schema, so the acp router correctly returns JSON-RPC -32601 to the
# caller — but the supervisor task that dispatches the request then surfaces
# the raised RequestError via ``logging.exception("Background task failed")``,
# which dumps a traceback to stderr every probe interval. Clients like
# acp-bridge already treat the -32601 response as "agent alive", so the
# traceback is pure noise. We keep the protocol response intact and only
# silence the stderr noise for this specific benign case.
_BENIGN_PROBE_METHODS = frozenset({"ping", "health", "healthcheck"})


class _BenignProbeMethodFilter(logging.Filter):
    """Suppress acp 'Background task failed' tracebacks caused by unknown
    liveness-probe methods (e.g. ``ping``) while leaving every other
    background-task error — including method_not_found for any non-probe
    method — visible in stderr.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != "Background task failed":
            return True
        exc_info = record.exc_info
        if not exc_info:
            return True
        exc = exc_info[1]
        # Imported lazily so this module stays importable when the optional
        # ``agent-client-protocol`` dependency is not installed.
        try:
            from acp.exceptions import RequestError
        except ImportError:
            return True
        if not isinstance(exc, RequestError):
            return True
        if getattr(exc, "code", None) != -32601:
            return True
        data = getattr(exc, "data", None)
        method = data.get("method") if isinstance(data, dict) else None
        return method not in _BENIGN_PROBE_METHODS


def _setup_logging() -> None:
    """Route all logging to stderr so stdout stays clean for ACP stdio."""
    from agent.redact import RedactingFormatter

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_BenignProbeMethodFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _load_env() -> None:
    """Load .env from HERMES_HOME (default ``~/.hermes``)."""
    from hermes_cli.env_loader import load_hermes_dotenv

    hermes_home = get_hermes_home()
    loaded = load_hermes_dotenv(hermes_home=hermes_home)
    if loaded:
        for env_file in loaded:
            logging.getLogger(__name__).info("Loaded env from %s", env_file)
    else:
        logging.getLogger(__name__).info(
            "No .env found at %s, using system env", hermes_home / ".env"
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hermes-acp",
        description="Run Hermes Agent as an ACP stdio server.",
    )
    parser.add_argument("--version", action="store_true", help="Print Hermes version and exit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify ACP dependencies and adapter imports, then exit",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run interactive Hermes provider/model setup for ACP terminal auth",
    )
    parser.add_argument(
        "--setup-browser",
        action="store_true",
        help="Install agent-browser + Playwright Chromium into ~/.hermes/node/ "
             "for browser tool support. Idempotent.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        dest="assume_yes",
        help="Accept all prompts (currently used by --setup-browser to skip the "
             "~400 MB Chromium download confirmation).",
    )
    return parser.parse_args(argv)


def _print_version() -> None:
    from hermes_cli import __version__ as hermes_version

    print(hermes_version)


def _run_check() -> None:
    import acp  # noqa: F401
    from acp_adapter.server import HermesACPAgent  # noqa: F401

    print("Hermes ACP check OK")


def _run_setup() -> None:
    from hermes_cli.main import main as hermes_main

    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0] if old_argv else "hermes", "model"]
        hermes_main()
    finally:
        sys.argv = old_argv

    # Offer browser-tools install as a follow-up. The terminal auth method
    # is the one supported first-run UX for registry installs, so this is
    # the natural moment to ask. Skip silently if stdin isn't a TTY (the
    # answer can't be collected anyway).
    if not sys.stdin.isatty():
        return
    try:
        reply = input(
            "\nInstall browser tools? Downloads agent-browser (npm) and "
            "optionally Playwright Chromium (~400 MB). [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if reply in {"y", "yes"}:
        _run_setup_browser(assume_yes=False)


def _run_setup_browser(assume_yes: bool = False) -> int:
    """Bootstrap agent-browser + Chromium.

    Routes through dep_ensure -> install.{sh,ps1} --ensure, sharing code
    with the runtime lazy installer.

    Returns 0 on success, 1 on failure.
    """
    from hermes_cli.dep_ensure import ensure_dependency

    try:
        node_ok = ensure_dependency("node", interactive=not assume_yes)
        if not node_ok:
            print("Node.js installation failed — cannot proceed with browser tools.",
                  file=sys.stderr)
            return 1

        browser_ok = ensure_dependency("browser", interactive=not assume_yes)
        if not browser_ok:
            print("Browser tools installation failed.", file=sys.stderr)
            return 1

        return 0
    except OSError as exc:
        print(f"Browser bootstrap failed: {exc}", file=sys.stderr)
        return 1


def _fd_mode(stream) -> int | None:
    """Return ``os.fstat(stream).st_mode`` or None when it can't be determined."""
    try:
        return os.fstat(stream.fileno()).st_mode
    except (AttributeError, OSError, ValueError):
        return None


def _is_pipe_like(stream) -> bool:
    """True when ``stream`` can back an asyncio pipe transport.

    The acp SDK's POSIX ``stdio_streams()`` calls ``loop.connect_read_pipe`` /
    ``loop.connect_write_pipe`` on the real stdio, which raises
    ``ValueError: Pipe transport is only for pipes, sockets and character devices``
    for anything that is not a pipe, socket, or character device (a redirected
    regular file, an in-memory wrapper, etc.).  This mirrors the mode check
    Python itself applies before building those transports.
    """
    mode = _fd_mode(stream)
    if mode is None:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode)


def _needs_stdio_fallback(platform: str | None = None) -> bool:
    """True when the ACP stdio transport needs Hermes-side stream pumps.

    The acp SDK only touches the real stdio with ``connect_read_pipe`` /
    ``connect_write_pipe`` on POSIX; on Windows it already uses a background
    stdin feeder plus a custom stdout transport that tolerates any stdout
    (``acp.stdio._windows_stdio_streams``).  So the fallback is only required
    on POSIX when either stdin or stdout is not pipe-like — e.g.
    ``hermes acp > out.txt`` or a host app that wraps stdout without a pipe.
    """
    if (sys.platform if platform is None else platform) == "win32":
        return False
    return not (_is_pipe_like(sys.stdin) and _is_pipe_like(sys.stdout))


class _StdoutWriteTransport(asyncio.BaseTransport):
    """Minimal write transport that forwards bytes to the real stdout.

    Used only when stdout is not pipe-like (redirected to a file, wrapped by
    a host app), where the SDK's ``connect_write_pipe`` would raise.  Mirrors
    ``acp.stdio._StdoutTransport``.
    """

    def __init__(self) -> None:
        self._is_closing = False

    def write(self, data: bytes) -> None:  # type: ignore[override]
        if self._is_closing:
            return
        try:
            target = getattr(sys.stdout, "buffer", sys.stdout)
            target.write(data)
            target.flush()
        except Exception:
            logging.exception("Error writing to stdout")

    def can_write_eof(self) -> bool:  # type: ignore[override]
        return False

    def is_closing(self) -> bool:  # type: ignore[override]
        return self._is_closing

    def close(self) -> None:  # type: ignore[override]
        self._is_closing = True
        with contextlib.suppress(Exception):
            sys.stdout.flush()

    def abort(self) -> None:  # type: ignore[override]
        self.close()

    def get_extra_info(self, name: str, default=None):  # type: ignore[override]
        return default


class _DrainHelperProtocol(asyncio.BaseProtocol):
    """Protocol shim so ``StreamWriter.drain()`` works over the fallback
    transport (it awaits ``protocol._drain_helper()``)."""

    def __init__(self) -> None:
        self._paused = False
        self._drain_waiter: asyncio.Future[None] | None = None

    def pause_writing(self) -> None:  # type: ignore[override]
        self._paused = True
        if self._drain_waiter is None:
            self._drain_waiter = asyncio.get_running_loop().create_future()

    def resume_writing(self) -> None:  # type: ignore[override]
        self._paused = False
        if self._drain_waiter is not None and not self._drain_waiter.done():
            self._drain_waiter.set_result(None)
        self._drain_waiter = None

    async def _drain_helper(self) -> None:
        if self._paused and self._drain_waiter is not None:
            await self._drain_waiter


def _build_fallback_stdio_streams(
    loop: asyncio.AbstractEventLoop,
    limit: int | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Build ``(reader, writer)`` that pump the real stdin/stdout without
    requiring pipe/socket/char-device backing.

    This is the same strategy the acp SDK already uses on Windows: a daemon
    thread feeds ``sys.stdin`` into an ``asyncio.StreamReader`` and a custom
    transport forwards ``StreamWriter.write()`` to ``sys.stdout.buffer``.  It
    lets ``hermes acp`` keep speaking JSON-RPC over stdio even when a host
    app redirects or wraps stdout (#84515).
    """
    reader = (
        asyncio.StreamReader(limit=limit)
        if limit is not None
        else asyncio.StreamReader()
    )

    def _feed_stdin() -> None:
        try:
            src = getattr(sys.stdin, "buffer", sys.stdin)
            while True:
                data = src.readline()
                if not data:
                    break
                loop.call_soon_threadsafe(reader.feed_data, data)
        finally:
            loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=_feed_stdin, daemon=True).start()

    write_protocol = _DrainHelperProtocol()
    transport = _StdoutWriteTransport()
    writer = asyncio.StreamWriter(transport, write_protocol, None, loop)
    return reader, writer


async def _run_acp_with_stdio_fallback(agent) -> None:
    """Run ``acp.run_agent`` over Hermes-built stdio pump streams.

    Used when the process stdio isn't pipe-backed (e.g. stdout redirected to
    a file), where the SDK's default stdio transport would raise
    ``ValueError: Pipe transport is only for pipes, sockets and character devices``.
    """
    import acp

    loop = asyncio.get_running_loop()
    reader, writer = _build_fallback_stdio_streams(loop)
    await acp.run_agent(
        agent,
        input_stream=writer,
        output_stream=reader,
        use_unstable_protocol=True,
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point: load env, configure logging, run the ACP agent."""
    args = _parse_args(argv)
    if args.version:
        _print_version()
        return
    if args.check:
        _run_check()
        return
    if args.setup:
        _run_setup()
        return
    if args.setup_browser:
        rc = _run_setup_browser(assume_yes=args.assume_yes)
        if rc != 0:
            sys.exit(rc)
        return

    _setup_logging()
    _load_env()

    logger = logging.getLogger(__name__)
    logger.info("Starting hermes-agent ACP adapter")

    # Ensure the project root is on sys.path so ``from run_agent import AIAgent`` works
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import acp
    from .server import HermesACPAgent

    # MCP tool discovery from config.yaml — fire-and-forget in a
    # background daemon thread so the ACP server becomes responsive
    # immediately while MCP servers connect.  Previously this blocked
    # asyncio.run() for 2-5 s.  (ACP also registers per-session MCP
    # servers dynamically via asyncio.to_thread inside the event loop;
    # that path is unaffected.)  Moved from model_tools.py module scope
    # to avoid freezing the gateway's loop on lazy import (#16856).
    # Metadata-only hosts can opt out of unrelated global MCP startup.
    if os.environ.get("HERMES_ACP_SKIP_CONFIGURED_MCP", "").strip() != "1":
        try:
            from hermes_cli.mcp_startup import start_background_mcp_discovery

            start_background_mcp_discovery(
                logger=logger,
                thread_name="acp-mcp-discovery",
            )
        except Exception:
            logger.debug("MCP tool discovery failed at ACP startup", exc_info=True)

    agent = HermesACPAgent()
    try:
        if _needs_stdio_fallback():
            stdin_mode = _fd_mode(sys.stdin)
            stdout_mode = _fd_mode(sys.stdout)
            logger.warning(
                "stdio is not pipe-backed (stdin mode %s, stdout mode %s); "
                "wrapping stdio in a Hermes-side pump so the ACP transport "
                "can start. Host applications should spawn `hermes acp` with "
                "stdio pipes.",
                oct(stdin_mode) if stdin_mode is not None else "n/a",
                oct(stdout_mode) if stdout_mode is not None else "n/a",
            )
            asyncio.run(_run_acp_with_stdio_fallback(agent))
        else:
            asyncio.run(acp.run_agent(agent, use_unstable_protocol=True))
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception:
        logger.exception("ACP agent crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
