
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


def get_gmail_service():
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        TOKEN_FILE.write_text(
            creds.to_json(),
            encoding="utf-8",
        )

        TOKEN_FILE.chmod(0o600)

    return build(
        "gmail",
        "v1",
        credentials=creds,
    )


if __name__ == "__main__":
    service = get_gmail_service()

    # Verify authorization without reading mailbox content.
    profile = service.users().getProfile(userId="me").execute()

    print("Gmail OAuth: SUCCESS")
    print("Authorized account:", profile["emailAddress"])
    print("Token saved:", TOKEN_FILE)
    print("No emails were sent.")
