"""Tests for _detect_tool_failure + _trim_error + get_cute_tool_message
inline failure suffix rendering.

Covers the user-visible promise: when a tool fails, the CLI shows a short,
specific reason in square brackets at the end of the completion line —
not a generic "[error]".
"""

import json
import logging

from agent.display import (
    _detect_tool_failure,
    _summarize_tool_failure,
    _trim_error,
    _ERROR_SUFFIX_MAX_LEN,
    get_cute_tool_message,
)
from agent.tool_executor import _log_tool_failure


class TestTrimError:
    """The helper that shrinks an error message for inline display."""

    def test_short_message_unchanged(self):
        assert _trim_error("nope") == "nope"


    def test_long_message_truncated_to_cap(self):
        msg = "x" * 200
        trimmed = _trim_error(msg)
        assert len(trimmed) <= _ERROR_SUFFIX_MAX_LEN
        assert trimmed.endswith("...")

    def test_file_not_found_path_collapsed_to_filename(self):
        long_path = "File not found: /home/teknium/.hermes/hermes-agent/very/deep/path/foo.py"
        assert _trim_error(long_path) == "File not found: foo.py"




class TestDetectToolFailureTerminal:
    """terminal: non-zero exit_code is the canonical failure signal."""

    def test_success_returns_no_suffix(self):
        result = json.dumps({"output": "ok\n", "exit_code": 0})
        assert _detect_tool_failure("terminal", result) == (False, "")


    def test_nonzero_exit_with_error_shows_message(self):
        result = json.dumps({
            "output": "",
            "exit_code": 127,
            "error": "ls: cannot access 'foo': No such file or directory",
        })
        is_failure, suffix = _detect_tool_failure("terminal", result)
        assert is_failure is True
        assert "cannot access" in suffix
        # Trimmed to the cap, in brackets
        assert suffix.startswith(" [")
        assert suffix.endswith("]")




class TestDetectToolFailureMemory:
    """memory: 'full' is distinct from real errors."""

    def test_memory_full_returns_full_suffix(self):
        result = json.dumps({"success": False, "error": "would exceed the limit"})
        assert _detect_tool_failure("memory", result) == (True, " [full]")

    def test_memory_other_error_returns_specific_message(self):
        # An error that's NOT a "full" overflow falls through to the
        # structured-error path and surfaces the actual message.
        result = json.dumps({"success": False, "error": "invalid action: zap"})
        is_failure, suffix = _detect_tool_failure("memory", result)
        assert is_failure is True
        assert "invalid action" in suffix


class TestDetectToolFailureStructured:
    """Generic path: any tool that returns {"error": ...} JSON."""

    def test_read_file_error_surfaced(self):
        result = json.dumps({
            "path": "/nope/missing.py",
            "success": False,
            "error": "File not found: /nope/missing.py",
        })
        is_failure, suffix = _detect_tool_failure("read_file", result)
        assert is_failure is True
        # _trim_error reduces the path to the basename.
        assert suffix == " [File not found: missing.py]"



    def test_successful_result_not_flagged(self):
        result = json.dumps({"success": True, "data": "hello"})
        assert _detect_tool_failure("web_search", result) == (False, "")

    def test_explicit_ok_true_is_not_failed_by_nested_error_metadata(self):
        result = json.dumps({
            "ok": True,
            "data": {
                "request_id": "video-123",
                "state": "completed",
                "error": {"code": "old_attempt", "message": "prior attempt failed"},
            },
        })
        assert _detect_tool_failure("siren_video_gen", result) == (False, "")

    def test_explicit_ok_true_with_failed_operation_is_failure(self):
        result = json.dumps({
            "ok": True,
            "data": {
                "request_id": "video-123",
                "state": "failed",
                "error": {"code": "permission_denied", "message": "provider returned 403"},
            },
        })
        assert _detect_tool_failure("siren_video_gen", result)[0] is True

    def test_explicit_ok_false_is_failure(self):
        result = json.dumps({
            "ok": False,
            "error": {"code": "request_conflict", "message": "request id is reserved"},
        })
        assert _detect_tool_failure("siren_video_gen", result)[0] is True


class TestSummarizeToolFailure:
    def test_summarizes_top_level_contract_error(self):
        result = json.dumps({
            "ok": False,
            "error": {"code": "request_conflict", "message": "request id is reserved"},
        })
        assert _summarize_tool_failure(result) == (
            "outcome=tool_error code=request_conflict message=request id is reserved"
        )

    def test_summarizes_failed_operation_receipt(self):
        result = json.dumps({
            "ok": True,
            "data": {
                "request_id": "video-123",
                "provider": "google",
                "model": "gemini-omni-flash-preview",
                "state": "failed",
                "error": {"code": "permission_denied", "message": "provider returned 403"},
            },
        })
        assert _summarize_tool_failure(result) == (
            "outcome=operation_failed request_id=video-123 provider=google "
            "model=gemini-omni-flash-preview state=failed code=permission_denied "
            "message=provider returned 403"
        )

    def test_falls_back_to_bounded_redacted_text(self):
        secret = "AIza" + ("x" * 35)
        result = f"Error {secret} " + ("x" * 600)
        summary = _summarize_tool_failure(result)
        assert secret not in summary
        assert len(summary) <= 500

    def test_structured_summary_is_bounded_and_redacted(self):
        secret = "AIza" + ("x" * 35)
        result = json.dumps({
            "ok": True,
            "data": {
                "request_id": "video-123",
                "provider": "google",
                "model": "gemini-omni-flash-preview",
                "state": "failed",
                "error": {
                    "code": "permission_denied",
                    "message": secret + (" details" * 100),
                },
            },
        })
        summary = _summarize_tool_failure(result)
        assert summary.startswith("outcome=operation_failed request_id=video-123")
        assert secret not in summary
        assert summary.endswith("...")
        assert len(summary) == 500


class TestLogToolFailure:
    def test_logs_structured_receipt_before_guardrail_annotation(self, caplog):
        result = json.dumps({
            "ok": True,
            "data": {
                "request_id": "video-123",
                "provider": "google",
                "model": "gemini-omni-flash-preview",
                "state": "failed",
                "error": {"code": "permission_denied", "message": "provider returned 403"},
            },
        })
        with caplog.at_level(logging.WARNING, logger="agent.tool_executor"):
            _log_tool_failure("siren_video_gen", 0.92, result)

        assert caplog.messages == [
            "Tool siren_video_gen failed (0.92s): outcome=operation_failed "
            "request_id=video-123 provider=google model=gemini-omni-flash-preview "
            "state=failed code=permission_denied message=provider returned 403"
        ]



class TestGetCuteToolMessageFailureSuffix:
    """End-to-end: failure suffix is appended by get_cute_tool_message."""




    def test_memory_full_suffix(self):
        fail = json.dumps({"success": False, "error": "would exceed the limit"})
        line = get_cute_tool_message(
            "memory",
            {"action": "add", "target": "memory", "content": "x"},
            0.05,
            result=fail,
        )
        assert "[full]" in line

    def test_success_has_no_suffix(self):
        ok = json.dumps({"success": True, "data": "hi"})
        line = get_cute_tool_message("web_search", {"query": "hi"}, 0.2, result=ok)
        assert "[" not in line.split("0.2s", 1)[1]
