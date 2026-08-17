import pytest

from utils import sanitized_loopback_endpoint


def test_sanitized_loopback_endpoint_preserves_generic_api_path():
    assert (
        sanitized_loopback_endpoint(
            "http://user:password@127.0.0.1:18080/v1?token=secret#fragment"
        )
        == "http://127.0.0.1:18080/v1"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/sk-local-secret/v1",
        "/token/abc123/v1",
        "/users/operator:password/v1",
    ],
)
def test_sanitized_loopback_endpoint_omits_unsafe_path(path):
    endpoint = sanitized_loopback_endpoint(f"http://localhost:8000{path}")

    assert endpoint == "http://localhost:8000"
