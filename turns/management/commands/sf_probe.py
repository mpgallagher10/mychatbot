"""
Validate Salesforce access and inspect a turn's context:

    python manage.py sf_probe a05PW00000n0k0DYAQ

Prints the turn's key fields (photo folder, walk date, status, tenant), the
resolved prior turn, and counts of prior findings / open maintenance items —
so the Work_Item__c mapping can be confirmed against a live record before a
full run. Requires SALESFORCE_* env vars.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from turns.services.salesforce_client import (
    TURN_DATE_FIELD,
    SalesforceClient,
    SalesforceError,
)


def _preview(value, n=140):
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


class Command(BaseCommand):
    help = "Inspect a turn Work_Item__c and its evaluation context."

    def add_arguments(self, parser):
        parser.add_argument("work_item_id", help="Turn Work_Item__c id")

    def handle(self, *args, **options):
        wid = options["work_item_id"]
        try:
            client = SalesforceClient()
        except RuntimeError as exc:
            raise CommandError(str(exc))

        try:
            ctx = client.fetch_context_for_turn(wid)
        except SalesforceError as exc:
            raise CommandError(str(exc))

        turn = ctx["turn"]
        self.stdout.write(self.style.SUCCESS(f"Turn {turn.get('Name')} ({wid})"))
        for f in [
            "Property__c",
            TURN_DATE_FIELD,
            "Complete_Date__c",
            "Status__c",
            "Current_Tenant__c",
            "Photos_Uploaded__c",
            "Photo_Folder_URL__c",
            "Drive_Link__c",
        ]:
            self.stdout.write(f"  {f}: {_preview(turn.get(f))}")

        self.stdout.write("\n  Condition notes:")
        for f in ["Problems_Found__c", "Maintenance_Issues_Observed__c", "Notes__c"]:
            self.stdout.write(f"    {f}: {_preview(turn.get(f))}")

        prop = ctx.get("property") or {}
        self.stdout.write(
            f"\nProperty: {prop.get('Name')} — {_preview(prop.get('Address_f__c'))}"
        )

        prior = ctx.get("prior_turns") or []
        self.stdout.write(f"\nPrior turns found: {len(prior)}")
        if prior:
            p = prior[0]
            self.stdout.write(
                f"  most recent prior: {p.get('Name')} @ {p.get(TURN_DATE_FIELD)}"
            )
            self.stdout.write(f"    Photo_Folder_URL__c: {_preview(p.get('Photo_Folder_URL__c'))}")

        self.stdout.write(
            f"\nPrior findings (maintenance items on prior turn): "
            f"{len(ctx.get('prior_findings') or [])}"
        )
        self.stdout.write(
            f"Open maintenance items on property: "
            f"{len(ctx.get('open_maintenance_items') or [])}"
        )
