"""
Validate Dropbox access against a real folder (path or shared link):

    python manage.py dropbox_probe "https://www.dropbox.com/scl/fo/.../...?rlkey=..."
    python manage.py dropbox_probe "/Turns/P-1/2026-07-16"

Lists the images the ingest worker would download and prints a small sample,
so shared-link listing can be confirmed with live credentials before wiring a
full run. Requires DROPBOX_* env vars to be set.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from turns.services.dropbox_client import (
    DropboxClient,
    is_shared_url,
    normalize_dropbox_shared_url,
)


class Command(BaseCommand):
    help = "List images under a Dropbox path or shared-link folder."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Dropbox account path or shared-link URL")
        parser.add_argument(
            "--download-first",
            action="store_true",
            help="Also download the first image to confirm read access.",
        )

    def handle(self, *args, **options):
        source = options["source"]
        if is_shared_url(source):
            self.stdout.write(
                f"normalized url: {normalize_dropbox_shared_url(source)}"
            )
        try:
            client = DropboxClient()
        except RuntimeError as exc:
            raise CommandError(str(exc))

        entries = client.list_images(source)
        self.stdout.write(self.style.SUCCESS(f"{len(entries)} image(s) found"))
        for e in entries[:10]:
            self.stdout.write(f"  - {e.name} ({e.size} bytes)")
        if len(entries) > 10:
            self.stdout.write(f"  ... and {len(entries) - 10} more")

        if options["download_first"] and entries:
            data = client.download(entries[0])
            self.stdout.write(
                self.style.SUCCESS(
                    f"downloaded {entries[0].name}: {len(data)} bytes OK"
                )
            )
