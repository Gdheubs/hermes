from hermes_cli.config import DEFAULT_CONFIG


class TestVerificationStrictDefaults:
    def test_verify_on_stop_default_is_disabled(self):
        assert DEFAULT_CONFIG["agent"]["verify_on_stop"] is False

    def test_max_verify_nudges_is_two(self):
        assert DEFAULT_CONFIG["agent"]["max_verify_nudges"] == 2
