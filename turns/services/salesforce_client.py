"""
Salesforce access: pull the context needed to evaluate a walk.

Schema (Texas Corporate Homes):
  * Property__c   — the managed home.
  * Work_Item__c  — polymorphic by RecordType:
        RT_TURN        = 012Kj000000ooNTIAY  -> a turn/walk (inspection)
        RT_MAINTENANCE = 012Kj000000ooNYIAY  -> a maintenance item

Key relationships:
  * Work_Item__c.Property__c            -> Property__c (both record types)
  * Work_Item__c.Related_Turn_Walk__c   -> the parent turn (maintenance items
                                           created from a walk link back here)
  * Work_Item__c.Photo_Folder_URL__c    -> the walk's photo folder (Dropbox/Drive)
  * Work_Item__c.Current_Tenant__c      -> Account to bill for guest damage

A turn's condition notes live on the turn record itself:
  * Problems_Found__c            (Condition Issues - Guest Related, rich text)
  * Maintenance_Issues_Observed__c
  * Carpet_Condition__c / Furniture_Condition__c / Odor_Description__c / Notes__c
  * Inventory_Used__c

`simple_salesforce` is imported lazily. The client returns plain dicts; the
ingest job stores them verbatim in `InspectionRun.salesforce_snapshot`.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Iterable, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# --- Org schema mapping -----------------------------------------------------
PROPERTY_OBJECT = "Property__c"
WORK_ITEM_OBJECT = "Work_Item__c"

# Fields to SELECT for a property (review-UI header + eval context).
PROPERTY_FIELDS = [
    "Id",
    "Name",              # Property Name (Master ID)
    "Address_f__c",
    "Street__c",
    "City__c",
    "State__c",
    "Zip_Code__c",
    "Beds__c",
    "Baths__c",
    "Sq_Ft__c",
    "Year_Built__c",
    "Property_Owner__c",
    "Quality_Rating__c",
    "Drive_Link__c",
]

RT_TURN = "012Kj000000ooNTIAY"          # a turn/walk (inspection)
RT_MAINTENANCE = "012Kj000000ooNYIAY"   # a maintenance item

# Field API name of the walk date used to order turns (Scheduled Date).
TURN_DATE_FIELD = "Date__c"

# Fields to SELECT for a turn/walk Work_Item__c.
TURN_FIELDS = [
    "Id",
    "Name",
    "RecordTypeId",
    "Property__c",
    "Property_Address__c",
    "Date__c",
    "Complete_Date__c",
    "Completed_Date_Time__c",
    "Status__c",
    "Current_Tenant__c",
    "Photo_Folder_URL__c",
    "Drive_Link__c",
    "Walk_Form_URL__c",
    "Photos_Uploaded__c",
    "Problems_Found__c",              # Condition Issues - Guest Related
    "Maintenance_Issues_Observed__c",
    "Carpet_Condition__c",
    "Furniture_Condition__c",
    "Odor_Description__c",
    "Notes__c",
    "Inventory_Used__c",
    "No_of_Open_Damage_Resolutions__c",
    "Total_Damage_Resolutions__c",
    "Deposit_Deduction_Items__c",
]

# Fields to SELECT for a maintenance Work_Item__c (a prior/current finding).
MAINTENANCE_FIELDS = [
    "Id",
    "Name",
    "RecordTypeId",
    "Property__c",
    "Related_Turn_Walk__c",
    "Status__c",
    "Billing_Status__c",
    "Issue_Type__c",
    "Type__c",
    "Priority__c",
    "Urgency_Color__c",
    "Description__c",
    "External_Desc_AI__c",
    "Current_Tenant__c",
    "Security_Deposit_Deduction__c",
    "Invoice_Total__c",
    "Parts__c",
    "CreatedDate",
]

# Status picklist values that count as an "open" maintenance item.
# TODO(confirm): validate against the Status__c picklist in the org.
OPEN_MAINTENANCE_STATUSES = ["Open", "In Progress", "New", "Assigned", "Scheduled"]

# Salesforce record ids are 15 or 18 char alphanumerics. Validate any id we
# interpolate into SOQL (webhook-supplied) to avoid injection.
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")


class SalesforceError(Exception):
    pass


def _safe_id(value: str) -> str:
    if not _SF_ID_RE.match(value or ""):
        raise SalesforceError(f"invalid Salesforce id: {value!r}")
    return value


def _select(fields: Iterable[str]) -> str:
    return ", ".join(fields)


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

    # --- Property -----------------------------------------------------------
    def get_property(self, property_id: str) -> Optional[dict]:
        pid = _safe_id(property_id)
        rows = self._query_all(
            f"SELECT {_select(PROPERTY_FIELDS)} FROM {PROPERTY_OBJECT} "
            f"WHERE Id = '{pid}' LIMIT 1"
        )
        return rows[0] if rows else None

    # --- Turns / walks ------------------------------------------------------
    def get_turn(self, work_item_id: str) -> Optional[dict]:
        """Fetch a single turn/walk Work_Item__c by id."""
        wid = _safe_id(work_item_id)
        rows = self._query_all(
            f"SELECT {_select(TURN_FIELDS)} FROM {WORK_ITEM_OBJECT} "
            f"WHERE Id = '{wid}' AND RecordTypeId = '{RT_TURN}' LIMIT 1"
        )
        return rows[0] if rows else None

    def get_prior_turns(
        self,
        property_id: str,
        *,
        before_date: Optional[date] = None,
        exclude_id: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Most recent prior turns for a property, newest first."""
        pid = _safe_id(property_id)
        where = [f"Property__c = '{pid}'", f"RecordTypeId = '{RT_TURN}'"]
        if before_date is not None:
            where.append(f"{TURN_DATE_FIELD} < {before_date.isoformat()}")
        if exclude_id:
            where.append(f"Id != '{_safe_id(exclude_id)}'")
        return self._query_all(
            f"SELECT {_select(TURN_FIELDS)} FROM {WORK_ITEM_OBJECT} "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {TURN_DATE_FIELD} DESC NULLS LAST LIMIT {limit}"
        )

    # --- Maintenance items (= findings) -------------------------------------
    def get_findings_for_turn(self, turn_id: str) -> list[dict]:
        """Maintenance items created from a given turn (its logged findings)."""
        tid = _safe_id(turn_id)
        return self._query_all(
            f"SELECT {_select(MAINTENANCE_FIELDS)} FROM {WORK_ITEM_OBJECT} "
            f"WHERE Related_Turn_Walk__c = '{tid}' "
            f"AND RecordTypeId = '{RT_MAINTENANCE}' ORDER BY CreatedDate DESC"
        )

    def get_open_maintenance_items(self, property_id: str) -> list[dict]:
        pid = _safe_id(property_id)
        statuses = ", ".join(f"'{s}'" for s in OPEN_MAINTENANCE_STATUSES)
        return self._query_all(
            f"SELECT {_select(MAINTENANCE_FIELDS)} FROM {WORK_ITEM_OBJECT} "
            f"WHERE Property__c = '{pid}' AND RecordTypeId = '{RT_MAINTENANCE}' "
            f"AND Status__c IN ({statuses}) ORDER BY CreatedDate DESC"
        )

    # --- Snapshots ----------------------------------------------------------
    def fetch_context(self, property_id: str) -> dict[str, Any]:
        """Property-scoped context (no current turn id available)."""
        logger.info("salesforce: fetching context for property %s", property_id)
        return {
            "property": self.get_property(property_id),
            "prior_turns": self.get_prior_turns(property_id),
            "open_maintenance_items": self.get_open_maintenance_items(property_id),
        }

    def fetch_context_for_turn(self, work_item_id: str) -> dict[str, Any]:
        """
        Full context anchored on the current turn Work_Item__c.

        Includes the current turn record (with its photo folder + condition
        notes), the immediately-prior turn and that turn's findings (for
        comparison), and all open maintenance items on the property.
        """
        logger.info("salesforce: fetching context for turn %s", work_item_id)
        turn = self.get_turn(work_item_id)
        if not turn:
            raise SalesforceError(f"turn Work_Item not found: {work_item_id}")

        property_id = turn.get("Property__c")
        turn_date = turn.get(TURN_DATE_FIELD)
        before = date.fromisoformat(turn_date) if turn_date else None

        prior_turns = (
            self.get_prior_turns(
                property_id, before_date=before, exclude_id=work_item_id, limit=5
            )
            if property_id
            else []
        )
        prior_findings = (
            self.get_findings_for_turn(prior_turns[0]["Id"]) if prior_turns else []
        )
        return {
            "turn": turn,
            "property": self.get_property(property_id) if property_id else None,
            "prior_turns": prior_turns,
            "prior_findings": prior_findings,
            "open_maintenance_items": (
                self.get_open_maintenance_items(property_id) if property_id else []
            ),
        }
