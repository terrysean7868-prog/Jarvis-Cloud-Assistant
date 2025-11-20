# src/ui/approval.py
import os
import json
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path

router = APIRouter()

# Storage folder for proposals
PROPOSAL_DIR = Path(os.getenv("PROPOSAL_DIR", "data/proposals"))
PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)

# Simple listing
@router.get("/approvals", response_class=HTMLResponse)
async def approvals_index(request: Request):
    items = []
    for p in sorted(PROPOSAL_DIR.glob("*.json"), reverse=True):
        data = json.loads(p.read_text(encoding="utf-8"))
        items.append({"id": p.stem, "summary": data.get("summary", ""), "when": data.get("when")})
    # render simple HTML
    rows = "".join([f"<li><a href='/approvals/{it['id']}'>{it['id']}</a> - {it['summary']} ({it['when']})</li>" for it in items])
    return HTMLResponse(f"<h1>Proposals</h1><ul>{rows}</ul>")

@router.get("/approvals/{pid}", response_class=HTMLResponse)
async def view_proposal(pid: str):
    p = PROPOSAL_DIR / f"{pid}.json"
    if not p.exists():
        return HTMLResponse("Not found", status_code=404)
    data = json.loads(p.read_text(encoding="utf-8"))
    html = f"<h1>Proposal {pid}</h1><pre>{json.dumps(data, indent=2)}</pre>"
    html += "<form method='post' action='/approvals/decide'><input type='hidden' name='pid' value='"+pid+"'/>"
    html += "<button name='decision' value='approve'>Approve</button>"
    html += "<button name='decision' value='reject'>Reject</button></form>"
    return HTMLResponse(html)

@router.post("/approvals/decide")
async def decide(pid: str = Form(...), decision: str = Form(...)):
    p = PROPOSAL_DIR / f"{pid}.json"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["decision"] = decision
    data["decided_at"] = datetime.utcnow().isoformat()
    p.write_text(json.dumps(data), encoding="utf-8")
    # If approved and contains 'apply_cmds', write them to a queue file for CI or apply runner
    if decision == "approve" and data.get("apply_cmds"):
        queue = PROPOSAL_DIR / "apply_queue.jsonl"
        queue.write_text((queue.read_text() if queue.exists() else "") + json.dumps({"pid": pid, "cmds": data.get("apply_cmds")}) + "\n")
    return JSONResponse({"status": "ok"})