"""
Gmail OAuth authorisation for the AccrualFlow pipeline.

The token file is the single source of truth for the Gmail authorisation.
It is refreshed **in place** and mounted read-write into the Airflow
container.

A refresh token can stop working for two very different reasons, and the
two need different responses:

  1. The token simply expired and the refresh succeeded.  This is the
     normal case and it is handled silently.
  2. The refresh token was **revoked**.  Then the account has to grant
     access again.  This module reports it as a clear, actionable error
     instead of an opaque ``invalid_grant`` traceback.

A refresh token issued to an OAuth app in **"Testing"** publishing status
stops working after **7 days**, no matter how often it is refreshed.  This
is a Google platform rule, not a bug, and it is the most common cause of a
token that "worked last week and is revoked today".  The moment of the
last consent is therefore recorded in the token file, so the module can
warn before the token dies instead of after.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = BASE_DIR / "config"

CREDENTIALS_FILE = CONFIG_DIR / "gmail_credentials.json"
TOKEN_FILE = CONFIG_DIR / "gmail_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# The key under which the moment of the last successful consent is stored
# inside the token file.  It is an extra key; the Google credential loader
# ignores unknown keys, so the token file stays a valid ``authorized_user``
# document.
GRANTED_AT_KEY = "accrualflow_granted_at"

# How long a refresh token issued by an OAuth app in "Testing" publishing
# status stays valid.  The refresh token itself is unaffected by the
# refreshes; the 7 day limit applies to the grant.
TESTING_MODE_REFRESH_TOKEN_LIFETIME = timedelta(days=7)

# How long before the suspected expiry the warning is raised.
EXPIRY_WARNING_LEAD = timedelta(days=1)


class GmailAuthorizationError(RuntimeError):
    """The Gmail authorisation is unusable and has to be renewed.

    This is raised when the refresh token was revoked, or when no usable
    token is present at all.  It is **not** a transient error: retrying
    will not help, the account has to grant access again.
    """

    def __init__(self, message: str, *, remediation: str | None = None):
        super().__init__(message)

        self.remediation = remediation or (
            "Re-authorise once in a browser:\n"
            "    python3 -m backend.app.services.gmail_auth_service"
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _read_token_document() -> dict:
    """Read the raw token document, or an empty mapping."""
    if not TOKEN_FILE.exists():
        return {}

    try:
        document = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    return document if isinstance(document, dict) else {}


def granted_at() -> datetime | None:
    """Return the moment of the current authorisation was granted.

    ``None`` is returned when the token file predates this bookkeeping, or
    when the token file cannot be read.
    """
    return _parse_timestamp(_read_token_document().get(GRANTED_AT_KEY))


def _testing_mode_window_remaining(
    now: datetime | None = None,
) -> timedelta | None:
    """Return how much of the 7 day testing-mode window is left.

    ``None`` is returned when the grant time is unknown, so that the caller
    can stay quiet instead of guessing.
    """
    granted = granted_at()

    if granted is None:
        return None

    now = now or _now()

    return granted + TESTING_MODE_REFRESH_TOKEN_LIFETIME - now


def describe_token_state(now: datetime | None = None) -> str:
    """Return a human readable summary of the token state.

    Used by the CLI entry point and by the health check, so that the reason
    a run failed can be read without reading a traceback.
    """
    now = now or _now()

    if not TOKEN_FILE.exists():
        return "No Gmail token file. The account has to grant access once."

    document = _read_token_document()

    if not document.get("refresh_token"):
        return (
            "The Gmail token file carries no refresh token. "
            "The account has to grant access again."
        )

    granted = _parse_timestamp(document.get(GRANTED_AT_KEY))

    if granted is None:
        return (
            "Gmail token present. The grant time is unknown, so the "
            "7 day testing-mode expiry cannot be predicted."
        )

    expires = granted + TESTING_MODE_REFRESH_TOKEN_LIFETIME

    if now >= expires:
        return (
            f"Gmail token granted at {granted.isoformat()} and the 7 day "
            f"testing-mode window ended at {expires.isoformat()}. "
            "The refresh token is expected to be revoked."
        )

    return (
        f"Gmail token granted at {granted.isoformat()}. "
        f"The 7 day testing-mode window ends at {expires.isoformat()} "
        f"({round((expires - now).total_seconds() / 3600, 1)} h left)."
    )


def _warn_about_imminent_expiry(now: datetime | None = None) -> None:
    """Print a warning when the testing-mode window is about to close.

    The window is only reported when it is known, so the message is never
    printed speculatively.
    """
    now = now or _now()

    remaining = _testing_mode_window_remaining(now)

    if remaining is None:
        return

    if remaining <= timedelta(0):
        return

    if remaining <= EXPIRY_WARNING_LEAD:
        print(
            "WARNING: the Gmail authorisation was granted "
            f"{round(remaining.total_seconds() / 3600, 1)} h ago and the "
            "OAuth app is in 'Testing' publishing status. Its refresh "
            "tokens stop working after 7 days. Re-authorise before the "
            "next scheduled run:\n"
            "    python3 -m backend.app.services.gmail_auth_service"
        )


def _is_revoked_token_error(exc: BaseException) -> bool:
    """Return True when the error means the refresh token was revoked.

    The check is made on the message, because it is the only part of the
    Google error that is stable across library versions.
    """
    return "invalid_grant" in str(exc)


def _refresh(creds):
    """Refresh the credentials, turning a revoked token into a clear error.

    A revoked refresh token cannot be repaired by the application.  The
    account has to grant access again, so the failure is reported as a
    dedicated exception that says exactly that.
    """
    try:
        creds.refresh(Request())
    except Exception as exc:  # noqa: BLE001 - classified below
        if not _is_revoked_token_error(exc):
            raise

        raise GmailAuthorizationError(
            "The Gmail refresh token was revoked or has expired "
            f"({exc}). The application cannot repair it; the account "
            "has to grant access again.",
        ) from exc

    return creds


def _persist(creds) -> None:
    """Write the credentials back to the token file, in place."""
    document = creds.to_json()

    try:
        parsed = json.loads(document)
    except ValueError:  # pragma: no cover - to_json() is always JSON
        parsed = None

    if isinstance(parsed, dict):
        parsed[GRANTED_AT_KEY] = _now().isoformat()
        document = json.dumps(parsed, indent=2)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    TOKEN_FILE.write_text(document, encoding="utf-8")

    TOKEN_FILE.chmod(0o600)


def get_gmail_service(allow_reauthorise: bool = False):
    """Return an authorised Gmail service.

    The token is refreshed in place.  When the refresh token has been
    revoked a :class:`GmailAuthorizationError` is raised, which names the
    remedy.

    ``allow_reauthorise`` makes a revoked token fall back to the
    interactive consent flow instead of raising.  That is what the CLI
    entry point needs: the human who runs it is there to re-authorise.
    A pipeline run leaves it at False, because a run must never open a
    browser and wait for someone.
    """
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds = _refresh(creds)
            except GmailAuthorizationError:
                if not allow_reauthorise:
                    raise

                # The refresh token is gone, so the only way forward is a
                # new consent.
                creds = None
        else:
            creds = None

    if not creds:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"OAuth credentials not found: {CREDENTIALS_FILE}"
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            SCOPES,
        )

        creds = flow.run_local_server(
            port=0,
            open_browser=True,
        )

        _persist(creds)

    _warn_about_imminent_expiry()

    return build(
        "gmail",
        "v1",
        credentials=creds,
    )


def verify_authorization(allow_reauthorise: bool = False) -> dict:
    """Check the authorisation and return the account profile.

    Nothing is sent and nothing is modified.
    """
    service = get_gmail_service(allow_reauthorise=allow_reauthorise)

    profile = service.users().getProfile(userId="me").execute()

    return {
        "email": profile.get("emailAddress"),
        "messages_total": profile.get("messagesTotal"),
        "threads_total": profile.get("threadsTotal"),
    }


if __name__ == "__main__":
    print("Token state:", describe_token_state())
    print()

    try:
        # This entry point is the explicit, human driven re-authorisation,
        # so a revoked token falls back to the interactive consent flow.
        profile = verify_authorization(allow_reauthorise=True)
    except GmailAuthorizationError as exc:
        print(f"Gmail OAuth: FAILED - {exc}")
        print()
        print(exc.remediation)
        raise SystemExit(1)

    print("Gmail OAuth: SUCCESS")
    print("Authorized account:", profile["email"])
    print("Token saved:", TOKEN_FILE)
    print("No emails were sent.")
