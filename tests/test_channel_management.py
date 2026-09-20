from datetime import UTC, datetime, timedelta

import pytest

from app.models import Channel
from app.services import channel_management

SIGNING_KEY = b"mock-local-signing-key"


@pytest.fixture
def channel() -> Channel:
    return Channel(
        id=10,
        username="example",
        created_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
    )


def test_delete_token_requires_matching_cookie_and_signing_key(channel: Channel) -> None:
    token = channel_management.deletion_token(channel, SIGNING_KEY)
    other_token = channel_management.deletion_token(channel, SIGNING_KEY)
    assert channel_management.valid_deletion_token(channel, token, token, SIGNING_KEY)
    assert not channel_management.valid_deletion_token(channel, token, None, SIGNING_KEY)
    assert not channel_management.valid_deletion_token(channel, token, other_token, SIGNING_KEY)
    assert not channel_management.valid_deletion_token(channel, token, token, b"different-signing-key")


@pytest.mark.parametrize(
    "hostile_input",
    [
        "",
        "підтвердження",
        "١٢٣." + "a" * 32 + "." + "b" * 64,
        "9" * 10000,
        "0." + "a" * 32 + "." + "🦊" * 64,
        "1.short.invalid",
        None,
        123,
    ],
)
def test_hostile_form_or_cookie_is_rejected_without_exception(
    channel: Channel, hostile_input: object
) -> None:
    token = channel_management.deletion_token(channel, SIGNING_KEY)
    assert not channel_management.valid_deletion_token(channel, hostile_input, token, SIGNING_KEY)
    assert not channel_management.valid_deletion_token(channel, token, hostile_input, SIGNING_KEY)


@pytest.mark.parametrize("age, expected", [(0, True), (600, True), (600.1, False), (-0.1, False)])
def test_delete_token_expiry_and_future_timestamps(
    channel: Channel, monkeypatch: pytest.MonkeyPatch, age: float, expected: bool
) -> None:
    monkeypatch.setattr(channel_management.time, "time", lambda: 1000)
    token = channel_management.deletion_token(channel, SIGNING_KEY)
    monkeypatch.setattr(channel_management.time, "time", lambda: 1000 + age)
    assert channel_management.valid_deletion_token(channel, token, token, SIGNING_KEY) is expected


@pytest.mark.parametrize("changed_field", ["id", "username", "created_at"])
def test_confirmation_cannot_delete_a_different_or_recreated_channel(
    channel: Channel, changed_field: str
) -> None:
    token = channel_management.deletion_token(channel, SIGNING_KEY)
    replacement = Channel(id=channel.id, username=channel.username, created_at=channel.created_at)
    if changed_field == "id":
        replacement.id += 1
    elif changed_field == "username":
        replacement.username = "another"
    else:
        # SQLite may reuse a deleted integer PK: creation identity must still bind the token.
        replacement.created_at += timedelta(microseconds=1)
    assert not channel_management.valid_deletion_token(replacement, token, token, SIGNING_KEY)


def test_same_shaped_forged_signature_is_rejected(channel: Channel) -> None:
    token = channel_management.deletion_token(channel, SIGNING_KEY)
    forged = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert not channel_management.valid_deletion_token(channel, forged, forged, SIGNING_KEY)
