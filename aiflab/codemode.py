"""Config C — "code mode" over the unchanged legacy API.

The model writes one Python script against an SDK for the legacy REST API; a sandbox
runs it, and only what the script prints comes back into the context. This is the
approach described in Anthropic's "Code execution with MCP" and Cloudflare's "Code Mode".

The scripts below are what a competent model would write for each question. Their
token count is charged as model output; their printed result is charged as a tool
result. Backend requests are those the script actually makes against the legacy API.
"""
from __future__ import annotations

import contextlib
import io
import json

HELPERS = '''import json
from datetime import date
TODAY = date(2026, 10, 1)
def rows(b): return json.loads(b)["data"]
def all_pages(fn, **kw):
    out, p = [], 1
    while True:
        b = json.loads(fn(page=p, page_size=20, **kw)); out += b["data"]
        if p >= b["meta"]["total_pages"]: return out
        p += 1
def d(ts): return date.fromisoformat(ts[:10])
req = {r["code"]: r for r in rows(api.get_reference_loss_types())}
'''

SCRIPTS = {
    "u1": HELPERS + '''c = rows(api.list_claims(claim_number={arg!r}))[0]
docs = all_pages(api.list_claim_documents, claim_id=c["id"])
notes = rows(api.list_claim_notes(c["id"]))  # newest first
have = {{x["doc_type_code"] for x in docs}}
last = max([d(n["created_at"]) for n in notes] + [d(x["uploaded_at"]) for x in docs])
print(json.dumps({{"missing": [x for x in req[c["loss_type_code"]]["required_documents"] if x not in have],
                  "days_since_activity": (TODAY - last).days}}))
''',
    "u2": HELPERS + '''a = next(a for a in rows(api.search_adjusters({arg!r})) if a["display_name"] == {arg!r})
out = []
for c in all_pages(api.list_claims, adjuster_id=a["id"], status="open,pending_info"):
    have = {{x["doc_type_code"] for x in all_pages(api.list_claim_documents, claim_id=c["id"])}}
    miss = [x for x in req[c["loss_type_code"]]["required_documents"] if x not in have]
    if miss: out.append([c["claim_number"], miss])
print(json.dumps(sorted(out)))
''',
    "u3": HELPERS + '''p = next(p for p in rows(api.search_policyholders({arg!r})) if p["first_name"] + " " + p["last_name"] == {arg!r})
claims = all_pages(api.list_claims, policyholder_id=p["id"])
paid = sum(x["amount"] for c in claims for x in all_pages(api.list_claim_payments, claim_id=c["id"]) if x["status"] == "paid")
print(json.dumps({{"reserved": round(sum(c["reserve"]["amount"] for c in claims), 2), "paid": round(paid, 2)}}))
''',
    "u4": HELPERS + '''t = next(t for t in rows(api.search_teams({arg!r})) if t["code"] == {arg!r})
out = []
for c in all_pages(api.list_claims, team_id=t["id"], status="open,pending_info"):
    acts = [n["created_at"] for n in rows(api.list_claim_notes(c["id"]))]
    acts += [x["uploaded_at"] for x in all_pages(api.list_claim_documents, claim_id=c["id"])]
    if (TODAY - max(d(x) for x in acts)).days > req[c["loss_type_code"]]["sla_days_no_activity"]:
        out.append(c["claim_number"])
print(json.dumps(sorted(out)))
''',
}


HELPERS_TUNED = HELPERS.replace("page_size=20", "page_size=100")

SCRIPTS_TUNED = {
    "u1": HELPERS_TUNED + '''c = rows(api.list_claims(claim_number={arg!r}, include=["documents", "notes"],
        fields={{"claims": ["claim_number", "loss_type_code"], "documents": ["doc_type_code", "uploaded_at"], "notes": ["created_at"]}}))[0]
have = {{x["doc_type_code"] for x in c["documents"]}}
last = max([d(n["created_at"]) for n in c["notes"]] + [d(x["uploaded_at"]) for x in c["documents"]])
print(json.dumps({{"missing": [x for x in req[c["loss_type_code"]]["required_documents"] if x not in have],
                  "days_since_activity": (TODAY - last).days}}))
''',
    "u2": HELPERS_TUNED + '''a = next(a for a in rows(api.search_adjusters({arg!r}, fields={{"adjusters": ["id", "display_name"]}})) if a["display_name"] == {arg!r})
out = []
for c in all_pages(api.list_claims, adjuster_id=a["id"], status="open,pending_info", include=["documents"],
                   fields={{"claims": ["claim_number", "loss_type_code"], "documents": ["doc_type_code"]}}):
    have = {{x["doc_type_code"] for x in c["documents"]}}
    miss = [x for x in req[c["loss_type_code"]]["required_documents"] if x not in have]
    if miss: out.append([c["claim_number"], miss])
print(json.dumps(sorted(out)))
''',
    "u3": HELPERS_TUNED + '''p = next(p for p in rows(api.search_policyholders({arg!r}, fields={{"policyholders": ["id", "first_name", "last_name"]}}))
         if p["first_name"] + " " + p["last_name"] == {arg!r})
claims = all_pages(api.list_claims, policyholder_id=p["id"], include=["payments"],
                   fields={{"claims": ["claim_number", "reserve"], "payments": ["amount", "status"]}})
paid = sum(x["amount"] for c in claims for x in c["payments"] if x["status"] == "paid")
print(json.dumps({{"reserved": round(sum(c["reserve"]["amount"] for c in claims), 2), "paid": round(paid, 2)}}))
''',
    "u4": HELPERS_TUNED + '''t = next(t for t in rows(api.search_teams({arg!r}, fields={{"teams": ["id", "code"]}})) if t["code"] == {arg!r})
out = []
for c in all_pages(api.list_claims, team_id=t["id"], status="open,pending_info", include=["notes", "documents"],
                   fields={{"claims": ["claim_number", "loss_type_code"], "notes": ["created_at"], "documents": ["uploaded_at"]}}):
    acts = [n["created_at"] for n in c["notes"]] + [x["uploaded_at"] for x in c["documents"]]
    if (TODAY - max(d(x) for x in acts)).days > req[c["loss_type_code"]]["sla_days_no_activity"]:
        out.append(c["claim_number"])
print(json.dumps(sorted(out)))
''',
}


def script_for(task: str, arg: str, tuned: bool = False) -> str:
    if tuned:
        return HELPERS_TUNED + SCRIPTS_TUNED[task][len(HELPERS_TUNED):].format(arg=arg)
    # HELPERS contains literal braces only inside the task bodies' escaped {{ }}; format the body only.
    body = SCRIPTS[task][len(HELPERS):].format(arg=arg)
    return HELPERS + body


def run(task, arg, api, conv, tuned=False):
    code = script_for(task, arg, tuned)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, f"<agent-{task}>", "exec"), {"api": api, "__name__": "__sandbox__"})
    stdout = buf.getvalue()
    conv.code_turn(code, stdout)
    ans = json.loads(stdout)
    conv.answer(json.dumps(ans))
    return ans
