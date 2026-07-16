"""
One-time helper to mint a Dropbox refresh token from the app key + secret.

App auth (key + secret alone) cannot list or download the contents of a shared
folder — that needs USER auth. Run this once in an interactive shell that has
network access to Dropbox, authorize the app, and paste the resulting
DROPBOX_REFRESH_TOKEN into the environment. Refresh tokens do not expire.

    python manage.py dropbox_login

Requires DROPBOX_APP_KEY and DROPBOX_APP_SECRET to be set.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Minimum scopes the ingest worker needs.
SCOPES = ["account_info.read", "files.metadata.read", "files.content.read", "sharing.read"]


class Command(BaseCommand):
    help = "Mint a Dropbox refresh token via the offline OAuth2 flow."

    def handle(self, *args, **options):
        if not (settings.DROPBOX_APP_KEY and settings.DROPBOX_APP_SECRET):
            raise CommandError("set DROPBOX_APP_KEY and DROPBOX_APP_SECRET first")

        from dropbox import DropboxOAuth2FlowNoRedirect

        flow = DropboxOAuth2FlowNoRedirect(
            settings.DROPBOX_APP_KEY,
            settings.DROPBOX_APP_SECRET,
            token_access_type="offline",
            scope=SCOPES,
        )
        authorize_url = flow.start()
        self.stdout.write("1. Visit this URL and click Allow:\n")
        self.stdout.write(self.style.NOTICE(f"   {authorize_url}\n"))
        self.stdout.write("2. Paste the authorization code below.\n")
        code = input("Authorization code: ").strip()

        try:
            result = flow.finish(code)
        except Exception as exc:  # noqa: BLE001 - surface the auth error verbatim
            raise CommandError(f"OAuth failed: {exc}")

        self.stdout.write(self.style.SUCCESS("\nSuccess. Set this in your environment:\n"))
        self.stdout.write(f"DROPBOX_REFRESH_TOKEN={result.refresh_token}\n")
