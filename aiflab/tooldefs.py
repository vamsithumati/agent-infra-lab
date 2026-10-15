"""Tool definitions the model sees for each configuration (counted as context tokens).

Descriptions are written to the same standard in every configuration: one or two
sentences, typed parameters, enums where they exist. None is padded or starved.
"""
from __future__ import annotations

import json

_page = {"page": {"type": "integer", "description": "1-based page number."},
         "page_size": {"type": "integer", "description": "Rows per page (max 20)."}}


def _tool(name, desc, props, required=()):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": list(required)}}


def legacy_tools(max_page=20, tuned=False):
    page = {"page": _page["page"], "page_size": {"type": "integer", "description": f"Rows per page (max {max_page})."}}
    shape = {}
    if tuned:
        shape = {"fields": {"type": "object", "description": "Sparse fieldsets: map of resource type to list of field names to return, e.g. {\"claims\": [\"claim_number\"]}.",
                            "additionalProperties": {"type": "array", "items": {"type": "string"}}}}
    inc = {"include": {"type": "array", "items": {"type": "string", "enum": ["documents", "notes", "payments"]},
                       "description": "Related resources to embed in each claim."}} if tuned else {}
    return [
        _tool("get_reference_loss_types", "List loss types with their required documents and no-activity SLA (days).", {}),
        _tool("search_adjusters", "Search adjusters by name. Returns adjuster records.", {"q": {"type": "string"}, **page, **shape}, ["q"]),
        _tool("search_teams", "Search claims teams by name or code.", {"q": {"type": "string"}, **page, **shape}, ["q"]),
        _tool("search_policyholders", "Search policyholders by name. Returns customer records.", {"q": {"type": "string"}, **page, **shape}, ["q"]),
        _tool("list_claims", "List claims, filtered. Status accepts a comma-separated list (open, pending_info, closed).",
              {"adjuster_id": {"type": "string"}, "team_id": {"type": "string"}, "policyholder_id": {"type": "string"},
               "status": {"type": "string"}, "claim_number": {"type": "string"}, **page, **shape, **inc}),
        _tool("get_claim", "Get one claim by id.", {"claim_id": {"type": "string"}, **shape, **inc}, ["claim_id"]),
        _tool("list_claim_documents", "List documents attached to a claim.", {"claim_id": {"type": "string"}, **page, **shape}, ["claim_id"]),
        _tool("list_claim_notes", "List notes on a claim, newest first.", {"claim_id": {"type": "string"}, **page, **shape}, ["claim_id"]),
        _tool("list_claim_payments", "List payments on a claim.", {"claim_id": {"type": "string"}, **page, **shape}, ["claim_id"]),
        _tool("get_policy", "Get one policy by id.", {"policy_id": {"type": "string"}, **shape}, ["policy_id"]),
    ]


def derived_tools():
    tools = legacy_tools(100, tuned=True)
    for t in tools:
        if t["name"] == "list_claims":
            t["description"] += (" Claims also carry derived fields: missing_documents, last_activity, days_since_activity, paid_total."
                                 " Filters: has_missing_documents (bool), stale (bool: no activity beyond the loss type's SLA).")
            t["input_schema"]["properties"]["has_missing_documents"] = {"type": "boolean"}
            t["input_schema"]["properties"]["stale"] = {"type": "boolean"}
    return tools


def native_tools():
    fmt = {"response_format": {"type": "string", "enum": ["concise", "detailed"], "description": "concise (default) or detailed."}}
    return [
        _tool("claim_brief", "What a claim needs next: status, missing required documents, last activity and SLA.",
              {"claim_number": {"type": "string", "description": "e.g. CLM-100123"}, **fmt}, ["claim_number"]),
        _tool("claims_missing_documents", "Claims that are missing required documents, for an adjuster or team (by name).",
              {"adjuster": {"type": "string"}, "team": {"type": "string"}, "active_only": {"type": "boolean"}}),
        _tool("policyholder_financials", "Reserved and paid totals across a policyholder's claims (by name).",
              {"name": {"type": "string"}}, ["name"]),
        _tool("stale_claims", "Active claims with no activity beyond their SLA (or beyond `days`), for a team (by name or code).",
              {"team": {"type": "string"}, "days": {"type": "integer"}, **fmt}, ["team"]),
        _tool("find_claims", "Find claims by policyholder, adjuster or team name. Returns compact rows.",
              {"policyholder": {"type": "string"}, "adjuster": {"type": "string"}, "team": {"type": "string"},
               "active_only": {"type": "boolean"}, "limit": {"type": "integer"}}),
    ]


CODE_MODE_SDK = '''Tool: run_python(code: str) -> stdout
Runs Python in a sandbox with a preloaded client `api` for the claims REST API.
Each method returns the JSON response body as a string (use json.loads). Lists are paginated:
{"data": [...], "meta": {"page", "page_size", "total", "total_pages"}}; max page_size 20.
Only what you print() is returned to you.

api.get_reference_loss_types() -> {"data": [{"code", "label", "required_documents": [str], "sla_days_no_activity": int}]}
api.search_adjusters(q, page=1, page_size=20)        -> adjusters: {"id", "display_name", "team_id", ...}
api.search_teams(q, page=1, page_size=20)            -> teams: {"id", "code", "name"}
api.search_policyholders(q, page=1, page_size=20)    -> policyholders: {"id", "first_name", "last_name", ...}
api.list_claims(page=1, page_size=20, adjuster_id=None, team_id=None, policyholder_id=None, status=None, claim_number=None)
    -> claims: {"id", "claim_number", "status", "loss_type_code", "policy_id", "reserve": {"amount"}, "audit": {...}, ...}
    status: comma-separated list of open, pending_info, closed
api.get_claim(claim_id)                               -> {"data": claim}
api.list_claim_documents(claim_id, page=1, page_size=20) -> documents: {"doc_type_code", "uploaded_at", ...}
api.list_claim_notes(claim_id, page=1, page_size=20)     -> notes, newest first: {"created_at", "body", ...}
api.list_claim_payments(claim_id, page=1, page_size=20)  -> payments: {"amount", "status", ...}
api.get_policy(policy_id)                             -> {"data": policy}
'''


CODE_MODE_SDK_TUNED = CODE_MODE_SDK.replace("max page_size 20.", "max page_size 100.").replace(
    "Only what you print() is returned to you.",
    "Only what you print() is returned to you.\nAll list/get methods accept fields={type: [field, ...]} (sparse fieldsets);"
    " list_claims and get_claim accept include=[\"documents\", \"notes\", \"payments\"] to embed related records.").replace(
    "page_size=20", "page_size=100")


def code_mode_tools(tuned=False):
    return [_tool("run_python", CODE_MODE_SDK_TUNED if tuned else CODE_MODE_SDK, {"code": {"type": "string"}}, ["code"])]


def as_text(tools) -> str:
    return json.dumps(tools, ensure_ascii=False)
