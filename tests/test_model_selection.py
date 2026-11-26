import pytest

from app.model_selection import ModelNotAvailable, resolve, split_reasoning_suffix
from tests.conftest import FakeClient, FakeModel


@pytest.mark.parametrize(
    "name,expected_base,expected_thinking",
    [
        ("gemini-flash", "gemini-flash", False),
        ("gemini-flash-high", "gemini-flash", True),
        ("gemini-pro-low", "gemini-pro", False),
        ("gemini-pro-medium", "gemini-pro", False),
        ("-high", "-high", False),  # too short to be only a suffix
    ],
)
def test_split_reasoning_suffix(name, expected_base, expected_thinking):
    assert split_reasoning_suffix(name) == (expected_base, expected_thinking)


def test_resolve_known_with_suffix():
    r = resolve(FakeClient(), "gemini-flash-high")
    assert r.served_name == "gemini-flash"
    assert r.extended_thinking is True


def test_resolve_unknown_lists_available():
    with pytest.raises(ModelNotAvailable) as ei:
        resolve(FakeClient(), "gpt-4o")
    assert "gemini-flash" in ei.value.available
    assert "gemini-pro" in ei.value.available


def test_resolve_fallback_prefers_gemini_auto():
    client = FakeClient()
    client._models = [
        FakeModel("gemini-pro", "bbb", ["pro"], is_available=False),
        FakeModel("gemini-auto", "ccc", ["auto"], is_available=True),
    ]
    r = resolve(client, "gemini-flash-high", fallback_to_default=True)
    assert r.served_name == "gemini-auto"
    assert r.extended_thinking is True


def test_resolve_auto_alias_falls_back_to_first_available():
    r = resolve(FakeClient(), "default-high")
    assert r.served_name == "gemini-flash"
    assert r.extended_thinking is True
