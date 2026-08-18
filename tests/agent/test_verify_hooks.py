from agent import verify_hooks


class TestCodingVerifyGuidance:
    def test_enabled_by_default(self):
        assert verify_hooks.coding_verify_guidance({}) == verify_hooks.CODING_VERIFY_GUIDANCE

    def test_requires_verification_evidence(self):
        assert "concrete verification evidence" in verify_hooks.CODING_VERIFY_GUIDANCE


class TestMaxVerifyNudges:
    def test_default_is_strict(self):
        assert verify_hooks.DEFAULT_MAX_VERIFY_NUDGES == 2


class TestVerifyOnStopDefaults:
    def test_default_is_disabled(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["agent"]["verify_on_stop"] is False

    def test_max_verify_nudges_default_is_two(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["agent"]["max_verify_nudges"] == 2
