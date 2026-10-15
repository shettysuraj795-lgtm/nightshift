"""NightShift API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.chaos import ChaosFlags, ChaosUpdate, get_flags, update_flags
from app.schemas import Failure, FailureDetail, TriageResult
from app.telemetry import flush, setup_tracing
from app.triage.pipeline import get_failure, get_pipeline, load_failures, read_log

APP_VERSION = "0.1.0"



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start sending spans to Phoenix when the server starts (not on import, so tests stay offline)."""
    setup_tracing()
    yield
    flush()


# FastAPI's own built-in telemetry is switched off, so no request details (such
# as caller addresses) reach traces or logs: no operator identity, by design.
# Each triage trace starts at the pipeline's `triage` span.
app = FastAPI(
    title="NightShift",
    description="AI triage assistant for failed overnight batch jobs (synthetic demo data).",
    version=APP_VERSION,
    telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False},
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the API process is up. Does not check Qdrant or the LLMs."""
    return {"status": "ok", "service": "nightshift", "version": APP_VERSION}


@app.get("/failures")
def list_failures() -> list[Failure]:
    """Last night's failed jobs, showcase scenarios S1-S4 first."""
    return load_failures()


@app.get("/failures/{failure_id}")
def failure_detail(failure_id: str) -> FailureDetail:
    """One failure with its raw log."""
    try:
        failure = get_failure(failure_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown failure {failure_id}") from None
    return FailureDetail(**failure.model_dump(), log_text=read_log(failure))


@app.post("/triage/{failure_id}")
def triage(failure_id: str) -> TriageResult:
    """Diagnose one failure. Blocking: takes a few seconds while the model answers."""
    try:
        failure = get_failure(failure_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown failure {failure_id}") from None
    return get_pipeline().run_log(failure.id, read_log(failure))


@app.get("/admin/chaos")
def chaos_flags() -> ChaosFlags:
    """Current outage simulation flags."""
    return get_flags()


@app.post("/admin/chaos")
def set_chaos_flags(update: ChaosUpdate) -> ChaosFlags:
    """Turn simulated outages on or off. Only the flags sent are changed; send all false to reset.

    Demo only: in a real deployment this endpoint would not exist or would sit behind admin auth.
    """
    return update_flags(update)
