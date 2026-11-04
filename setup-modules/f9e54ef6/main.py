"""Setup module — FastAPI app called by the BullsPlace setup engine (see the contract in the issue)."""

from fastapi import FastAPI, HTTPException, Request

import rules

app = FastAPI(title="Setup module")

# Declare the setup's own tunable inputs (parameters) and special views (panels) here; the platform renders them.
MANIFEST = {"parameters": [], "panels": []}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/manifest")
def manifest():
    return MANIFEST


@app.post("/evaluate")
async def evaluate(request: Request):
    payload = await request.json()
    return rules.evaluate(payload)


@app.post("/panels/{panel_id}")
async def panel(panel_id: str, request: Request):
    raise HTTPException(status_code=404, detail=f"Unknown panel {panel_id}")
