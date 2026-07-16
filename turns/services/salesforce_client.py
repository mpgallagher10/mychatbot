"""
Salesforce access: pull the context needed to evaluate a walk.

`simple_salesforce` is imported lazily. Object/field API names are centralized
in `SF_SCHEMA` below so they can be mapped to Texas Corporate Homes' actual org
without touching query logic. Adjust these to match the org (some may be
custom objects with __c suffixes).

The client returns plain dicts; the ingest job stores them verbatim in
`InspectionRun.salesforce_snapshot` so a run stays reproducible even if
Salesforce data changes afterward.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# --- Org schema mapping -----------------------------------------------------
# TODO(TCH): confirm these API names against the production org before go-live.
SF_SCHEMA = {
    "property_object": "Property__c",
    "inspection_object": "Inspection__c",       # one row per prior walk
    "finding_object": "Inspection_Finding__c",  # prior findings
    "ticket_object": "Case",                     # maintenance tickets
    "inventory_object": "Property_Inventory__c", # baseline item list
    "booking_object": "Booking__c",              # current guest/booking
}


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

    def get_property(self, salesforce_property_id: str) -> Optional[dict]:
        obj = SF_SCHEMA["property_object"]
        rows = self._query_all(
            f"SELECT Id, Name FROM {obj} WHERE Id = '{salesforce_property_id}' LIMIT 1"
        )
        return rows[0] if rows else None

    def get_prior_findings(self, salesforce_property_id: str, limit: int = 200) -> list[dict]:
        obj = SF_SCHEMA["finding_object"]
        prop = SF_SCHEMA["property_object"]
        return self._query_all(
            f"SELECT Id, Name, Category__c, Description__c, Severity__c, "
            f"Billable__c, Created_On_Walk__c "
            f"FROM {obj} WHERE Property__c = '{salesforce_property_id}' "
            f"ORDER BY CreatedDate DESC LIMIT {limit}"
        )

    def get_open_tickets(self, salesforce_property_id: str) -> list[dict]:
        obj = SF_SCHEMA["ticket_object"]
        return self._query_all(
            f"SELECT Id, CaseNumber, Subject, Status, Priority "
            f"FROM {obj} WHERE Property__c = '{salesforce_property_id}' "
            f"AND IsClosed = false ORDER BY CreatedDate DESC"
        )

    def get_inventory(self, salesforce_property_id: str) -> list[dict]:
        obj = SF_SCHEMA["inventory_object"]
        return self._query_all(
            f"SELECT Id, Name, Room__c, Quantity__c, Baseline_Value__c "
            f"FROM {obj} WHERE Property__c = '{salesforce_property_id}'"
        )

    def get_current_booking(self, salesforce_property_id: str) -> Optional[dict]:
        obj = SF_SCHEMA["booking_object"]
        rows = self._query_all(
            f"SELECT Id, Name, Guest_Name__c, Check_In__c, Check_Out__c "
            f"FROM {obj} WHERE Property__c = '{salesforce_property_id}' "
            f"ORDER BY Check_Out__c DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def fetch_context(self, salesforce_property_id: str) -> dict[str, Any]:
        """Pull the full evaluation context snapshot for a property."""
        logger.info("salesforce: fetching context for property %s", salesforce_property_id)
        return {
            "property": self.get_property(salesforce_property_id),
            "prior_findings": self.get_prior_findings(salesforce_property_id),
            "open_tickets": self.get_open_tickets(salesforce_property_id),
            "inventory": self.get_inventory(salesforce_property_id),
            "current_booking": self.get_current_booking(salesforce_property_id),
        }
