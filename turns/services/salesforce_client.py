"""
Salesforce access: pull the context needed to evaluate a walk.

Schema (Texas Corporate Homes):
  * Property__c                         — the managed home.
  * Work_Item__c                        — polymorphic by RecordType:
        RT_TURN        = 012Kj000000ooNTIAY  -> a turn/walk (inspection)
        RT_MAINTENANCE = 012Kj000000ooNYIAY  -> a maintenance item
    Prior walks and maintenance tickets are therefore both Work_Item__c rows,
    distinguished by RecordTypeId.

`simple_salesforce` is imported lazily. Object/field API names live in the
config block below so they map to the real org without touching query logic.

Field-level API names marked `# TODO(confirm)` are placeholders until the
Work_Item__c / Property__c field lists are confirmed — see get_prior_turns /
get_maintenance_items. The client returns plain dicts; the ingest job stores
them verbatim in `InspectionRun.salesforce_snapshot` so a run stays
reproducible even if Salesforce data changes afterward.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# --- Org schema mapping -----------------------------------------------------
PROPERTY_OBJECT = "Property__c"
WORK_ITEM_OBJECT = "Work_Item__c"

# Work_Item__c record types.
RT_TURN = "012Kj000000ooNTIAY"          # a turn/walk (inspection)
RT_MAINTENANCE = "012Kj000000ooNYIAY"   # a maintenance item

# Field API names on Work_Item__c. Confirm against the org before go-live.
WI_FIELDS = {
    "property_lookup": "Property__c",   # TODO(confirm) lookup to Property__c
    "walk_date": "Walk_Date__c",        # TODO(confirm) date of the turn/walk
    "status": "Status__c",              # TODO(confirm) for open-vs-closed
    # Extra fields to SELECT are appended in _select_fields() below once the
    # Work_Item__c field list is confirmed.
    "select": [
        "Id",
        "Name",
        # TODO(confirm): add description/category/severity/billable/cost fields
    ],
}

# Statuses that count as an "open" maintenance ticket. Confirm real picklist.
OPEN_STATUSES = ["Open", "In Progress", "New"]  # TODO(confirm)


def _quote_list(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class SalesforceClient:
    def __init__(self, sf=None):
        self._sf = sf or self._build_client()

    @staticmethod
    def _build_client():
        # Check configuration BEFORE importing the SDK so an unconfigured
        # environment raises a clean RuntimeError (which callers treat as
        # "skip Salesforce") rather than an ImportError.
        if not (settings.SALESFORCE_USERNAME and settings.SALESFORCE_PASSWORD):
            raise RuntimeError(
                "Salesforce not configured: set SALESFORCE_USERNAME, "
                "SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN"
            )
        from simple_salesforce import Salesforce

        return Salesforce(
            username=settings.SALESFORCE_USERNAME,
            password=settings.SALESFORCE_PASSWORD,
            security_token=settings.SALESFORCE_SECURITY_TOKEN,
            domain=settings.SALESFORCE_DOMAIN or "login",
        )

    def _query_all(self, soql: str) -> list[dict[str, Any]]:
        records = self._sf.query_all(soql).get("records", [])
        # Strip Salesforce's per-record `attributes` metadata for clean JSON.
        return [{k: v for k, v in r.items() if k != "attributes"} for r in records]

    @staticmethod
    def _work_item_select() -> str:
        # RecordTypeId + configured fields, de-duplicated, comma-joined.
        fields = ["Id", "Name", "RecordTypeId", WI_FIELDS["status"], WI_FIELDS["walk_date"]]
        for extra in WI_FIELDS["select"]:
            if extra not in fields:
                fields.append(extra)
        return ", ".join(fields)

    # --- Property -----------------------------------------------------------
    def get_property(self, salesforce_property_id: str) -> Optional[dict]:
        rows = self._query_all(
            f"SELECT Id, Name FROM {PROPERTY_OBJECT} "
            f"WHERE Id = '{salesforce_property_id}' LIMIT 1"
        )
        return rows[0] if rows else None

    # --- Prior turns/walks (for photo + finding comparison) -----------------
    def get_prior_turns(self, salesforce_property_id: str, limit: int = 10) -> list[dict]:
        """Most recent prior turn Work_Items for this property, newest first."""
        prop_field = WI_FIELDS["property_lookup"]
        date_field = WI_FIELDS["walk_date"]
        return self._query_all(
            f"SELECT {self._work_item_select()} FROM {WORK_ITEM_OBJECT} "
            f"WHERE {prop_field} = '{salesforce_property_id}' "
            f"AND RecordTypeId = '{RT_TURN}' "
            f"ORDER BY {date_field} DESC NULLS LAST LIMIT {limit}"
        )

    # --- Maintenance items --------------------------------------------------
    def get_maintenance_items(
        self, salesforce_property_id: str, open_only: bool = True
    ) -> list[dict]:
        prop_field = WI_FIELDS["property_lookup"]
        status_field = WI_FIELDS["status"]
        where = [
            f"{prop_field} = '{salesforce_property_id}'",
            f"RecordTypeId = '{RT_MAINTENANCE}'",
        ]
        if open_only:
            where.append(f"{status_field} IN ({_quote_list(OPEN_STATUSES)})")
        return self._query_all(
            f"SELECT {self._work_item_select()} FROM {WORK_ITEM_OBJECT} "
            f"WHERE {' AND '.join(where)} ORDER BY CreatedDate DESC"
        )

    # --- Full snapshot ------------------------------------------------------
    def fetch_context(self, salesforce_property_id: str) -> dict[str, Any]:
        """Pull the full evaluation context snapshot for a property."""
        logger.info("salesforce: fetching context for property %s", salesforce_property_id)
        return {
            "property": self.get_property(salesforce_property_id),
            "prior_turns": self.get_prior_turns(salesforce_property_id),
            "open_maintenance_items": self.get_maintenance_items(
                salesforce_property_id, open_only=True
            ),
        }
