"""NightShift API.

Phase 0 exposes only a health check. Triage endpoints arrive in Phase 2.
"""

from fastapi import FastAPI

APP_VERSION = "0.1.0"

app = FastAPI(
    title="NightShift",
    description="AI triage assistant for failed overnight batch jobs (synthetic demo data).",
    version=APP_VERSION,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the API process is up. Does not check Qdrant or the LLMs."""
    return {"status": "ok", "service": "nightshift", "version": APP_VERSION}
