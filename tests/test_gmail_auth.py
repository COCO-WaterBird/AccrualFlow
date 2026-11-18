"""
Tests for the Gmail authorisation service.

The behaviour under test is the distinction between a token that simply
expired and a token that was **revoked**, plus the bookkeeping that makes
the 7 day testing-mode expiry predictable instead of surprising.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


from backend.app.services import gmail_auth_service as auth


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    """Point the service at a throwaway token file."""
    token = tmp_path / "gmail_token.json"

    monkeypatch.setattr(auth, "TOKEN_FILE", token)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)

    return token


def _write_token(token: Path, *, granted_at=None, refresh_token="refresh"):
    document = {
        "token": "access",
        "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client",
        "client_secret": "secret",
        "scopes": list(auth.SCOPES),
        "expiry": "2026-11-17T01:04:10Z",
    }

    if granted_at is not None:
        document[auth.GRANTED_AT_KEY] = granted_at.isoformat()

    token.write_text(json.dumps(document), encoding="utf-8")

    return document


# ---------------------------------------------------------------------------
# The granted-at bookkeeping
# ---------------------------------------------------------------------------


def test_granted_at_is_none_when_the_token_file_is_missing(token_file):
    assert auth.granted_at() is None


def test_granted_at_is_none_when_the_token_predates_the_bookkeeping(token_file):
    _write_token(token_file)

    assert auth.granted_at() is None


def test_granted_at_is_read_from_the_token_file(token_file):
    granted = datetime(2026, 11, 10, 5, 49, 14, tzinfo=timezone.utc)

    _write_token(token_file, granted_at=granted)

    assert auth.granted_at() == granted


def test_an_unreadable_token_file_does_not_raise(token_file):
    token_file.write_text("not json at all", encoding="utf-8")

    assert auth.granted_at() is None


def test_a_timestamp_without_a_timezone_is_read_as_utc(token_file):
    _write_token(token_file, granted_at=datetime(2026, 11, 10, 5, 49, 14))

    assert auth.granted_at() == datetime(
        2026, 11, 10, 5, 49, 14, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# The 7 day testing-mode window
# ---------------------------------------------------------------------------


def test_the_testing_mode_window_is_seven_days(token_file):
    granted = datetime(2026, 11, 10, 5, 49, 14, tzinfo=timezone.utc)

    _write_token(token_file, granted_at=granted)

    remaining = auth._testing_mode_window_remaining(
        now=datetime(2026, 11, 16, 5, 49, 14, tzinfo=timezone.utc),
    )

    assert remaining == timedelta(days=1)


def test_the_testing_mode_window_is_unknown_without_a_grant_time(token_file):
    _write_token(token_file)

    assert auth._testing_mode_window_remaining() is None


def test_the_testing_mode_window_has_expired_after_seven_days(token_file):
    granted = datetime(2026, 11, 10, 5, 49, 14, tzinfo=timezone.utc)

    _write_token(token_file, granted_at=granted)

    remaining = auth._testing_mode_window_remaining(
        now=datetime(2026, 11, 17, 5, 49, 15, tzinfo=timezone.utc),
    )

    assert remaining < timedelta(0)


# ---------------------------------------------------------------------------
# The state description
# ---------------------------------------------------------------------------


def test_the_state_reports_a_missing_token_file(token_file):
    description = auth.describe_token_state()

    assert "No Gmail token file" in description


def test_the_state_reports_a_missing_refresh_token(token_file):
    _write_token(token_file, refresh_token="")

    description = auth.describe_token_state()

    assert "no refresh token" in description


def test_the_state_warns_when_the_window_has_closed(token_file):
    granted = datetime(2026, 11, 10, 5, 49, 14, tzinfo=timezone.utc)

    _write_token(token_file, granted_at=granted)

    description = auth.describe_token_state(
        now=datetime(2026, 11, 20, tzinfo=timezone.utc),
    )

    assert "testing-mode window ended" in description
    assert "expected to be revoked" in description


def test_the_state_reports_the_remaining_time(token_file):
    granted = datetime(2026, 11, 10, 5, 49, 14, tzinfo=timezone.utc)

    _write_token(token_file, granted_at=granted)

    description = auth.describe_token_state(
        now=datetime(2026, 11, 16, tzinfo=timezone.utc),
    )

    assert "ends at" in description


def test_the_state_stays_quiet_when_the_grant_time_is_unknown(token_file):
    _write_token(token_file)

    description = auth.describe_token_state()

    assert "grant time is unknown" in description


# ---------------------------------------------------------------------------
# The warning
# ---------------------------------------------------------------------------


def test_no_warning_is_raised_when_the_grant_time_is_unknown(token_file, capsys):
    _write_token(token_file)

    auth._warn_about_imminent_expiry()

    assert "WARNING" not in capsys.readouterr().out


def test_a_warning_is_raised_inside_the_lead_time(token_file, capsys):
    granted = datetime.now(timezone.utc) - timedelta(days=6, hours=23)

    _write_token(token_file, granted_at=granted)

    auth._warn_about_imminent_expiry()

    assert "WARNING" in capsys.readouterr().out


def test_no_warning_is_raised_outside_the_lead_time(token_file, capsys):
    granted = datetime.now(timezone.utc) - timedelta(days=2)

    _write_token(token_file, granted_at=granted)

    auth._warn_about_imminent_expiry()

    assert "WARNING" not in capsys.readouterr().out


def test_no_warning_is_raised_after_the_window_closed(token_file, capsys):
    granted = datetime.now(timezone.utc) - timedelta(days=9)

    _write_token(token_file, granted_at=granted)

    auth._warn_about_imminent_expiry()

    assert "WARNING" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The revoked token
# ---------------------------------------------------------------------------


def test_a_revoked_token_raises_an_actionable_error(token_file, monkeypatch):
    from google.auth.exceptions import RefreshError

    # The token file has to exist, otherwise the service falls through to
    # the interactive consent flow and waits for a human in a browser.
    _write_token(token_file)

    class FakeCreds:
        expired = True
        valid = False
        refresh_token = "refresh"

        def refresh(self, request):
            raise RefreshError(
                "invalid_grant: Token has been expired or revoked."
            )

    monkeypatch.setattr(
        auth,
        "Credentials",
        type(
            "C",
            (),
            {
                "from_authorized_user_file": staticmethod(
                    lambda *args, **kwargs: FakeCreds()
                )
            },
        ),
    )

    with pytest.raises(auth.GmailAuthorizationError) as excinfo:
        auth.get_gmail_service()

    assert "revoked" in str(excinfo.value)
    assert "gmail_auth_service" in excinfo.value.remediation


def test_a_transient_refresh_error_is_not_swallowed(token_file, monkeypatch):
    from google.auth.exceptions import RefreshError

    # The token file has to exist, otherwise the service falls through to
    # the interactive consent flow and waits for a human in a browser.
    _write_token(token_file)

    class FakeCreds:
        expired = True
        valid = False
        refresh_token = "refresh"

        def refresh(self, request):
            raise RefreshError("transport error: connection reset")

    monkeypatch.setattr(
        auth,
        "Credentials",
        type(
            "C",
            (),
            {
                "from_authorized_user_file": staticmethod(
                    lambda *args, **kwargs: FakeCreds()
                )
            },
        ),
    )

    with pytest.raises(RefreshError) as excinfo:
        auth.get_gmail_service()

    assert "invalid_grant" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The persistence
# ---------------------------------------------------------------------------


def test_persisting_the_credentials_records_the_grant_time(token_file):
    class FakeCreds:
        def to_json(self):
            return json.dumps(
                {
                    "token": "access",
                    "refresh_token": "refresh",
                }
            )

    auth._persist(FakeCreds())

    document = json.loads(token_file.read_text(encoding="utf-8"))

    assert auth.GRANTED_AT_KEY in document
    assert auth.granted_at() is not None


def test_the_persisted_token_file_is_owner_only(token_file):
    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "access"})

    auth._persist(FakeCreds())

    mode = token_file.stat().st_mode & 0o777

    assert mode == 0o600
