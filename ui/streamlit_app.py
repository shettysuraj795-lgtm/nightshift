"""NightShift demo UI: last night's failures, triage results and the approval flow.

The UI only talks to the NightShift API (`API_BASE_URL`); everything it shows
comes from API responses (see ui/view_model.py). Nothing is executed: the
approval flow only records a decision in this browser session.

Run with `make ui` (the API must be running: `make api`).
"""

import time
from datetime import datetime

import requests
import streamlit as st

from app.config import get_settings
from app.registry import load_registry
from app.schemas import Failure, FailureDetail, TriageResult
from app.telemetry import phoenix_trace_url
from ui.view_model import (ACTION_ICONS, ACTION_LABELS, banners, card_badge, clock_time, group_citations, risk_badge,
                           risk_explanation, summary_counts)

# The sidebar only holds the demo controls, so it starts collapsed: the presenter opens it when needed.
st.set_page_config(page_title="NightShift · Overnight job triage", page_icon=":material/nightlight:",
                   layout="wide", initial_sidebar_state="collapsed")

SETTINGS = get_settings()
API = SETTINGS.api_base_url.rstrip("/")
CATEGORY_TITLES = {mode.category: mode.title for mode in load_registry()}
CHAOS_FLAGS = {
    "llm_down": "Simulate AI provider outage",
    "vector_db_down": "Simulate knowledge base outage",
    "all_llms_down": "Simulate all AI models down",
}
BANNER = {"info": st.info, "success": st.success, "warning": st.warning, "error": st.error}

# Light CSS on top of the theme in .streamlit/config.toml.
st.markdown(
    """
    <style>
      .block-container { padding-top: 4.2rem; max-width: 1440px; }
      /* Streamlit styles markdown paragraphs itself, so these use divs and win with !important. */
      div.ns-title { font-size: 2.1rem !important; font-weight: 750; letter-spacing: -0.02em; line-height: 1.1; }
      div.ns-subtitle { color: #4B5563; font-size: 1.02rem; margin-top: 0.25rem; }
      span.ns-demo { display: inline-block; margin: 0.5rem 0 0.9rem 0; padding: 0.12rem 0.65rem; border-radius: 999px;
                     background: #FEF3C7; color: #92400E; font-size: 0.78rem; font-weight: 600; }
      div.ns-job { font-weight: 650; font-size: 0.92rem; margin-bottom: 0.15rem; word-break: break-word; }
      div.ns-meta { color: #6B7280; font-size: 0.84rem; }
      div.ns-heading { font-size: 1.45rem !important; font-weight: 700; line-height: 1.25; word-break: break-word; }
      div.ns-root { font-size: 1.18rem !important; font-weight: 600; line-height: 1.45; margin: 0.5rem 0 0.5rem 0; color: #111827; }
      div[data-testid="stCode"] code { font-size: 0.8rem !important; }
      div.ns-section { font-size: 1.02rem; font-weight: 650; margin: 1.1rem 0 0.35rem 0; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- API calls ----------------------------------------------------------------


def api_is_up() -> bool:
    try:
        return requests.get(f"{API}/health", timeout=3).ok
    except requests.RequestException:
        return False


@st.cache_data(ttl=60, show_spinner=False)
def load_failures() -> list[Failure]:
    response = requests.get(f"{API}/failures", timeout=10)
    response.raise_for_status()
    return [Failure.model_validate(item) for item in response.json()]


@st.cache_data(ttl=60, show_spinner=False)
def load_failure_detail(failure_id: str) -> FailureDetail:
    response = requests.get(f"{API}/failures/{failure_id}", timeout=10)
    response.raise_for_status()
    return FailureDetail.model_validate(response.json())


def run_triage(failure_id: str) -> tuple[TriageResult, float]:
    """Call the API and time it. The local backup model can be slow, so the timeout is generous."""
    started = time.perf_counter()
    response = requests.post(f"{API}/triage/{failure_id}", timeout=300)
    response.raise_for_status()
    return TriageResult.model_validate(response.json()), time.perf_counter() - started


def push_chaos(flag: str) -> None:
    """Toggle callback: send the new value of one chaos flag to the API."""
    requests.post(f"{API}/admin/chaos", json={flag: st.session_state[f"chaos_{flag}"]}, timeout=5)


def reset_demo() -> None:
    """Button callback: all outages off, results and approvals cleared."""
    requests.post(f"{API}/admin/chaos", json={flag: False for flag in CHAOS_FLAGS}, timeout=5)
    for flag in CHAOS_FLAGS:
        st.session_state[f"chaos_{flag}"] = False
    st.session_state.results.clear()
    st.session_state.approvals.clear()


# --- Session state ------------------------------------------------------------

st.session_state.setdefault("results", {})  # failure ID -> (TriageResult, seconds)
st.session_state.setdefault("approvals", {})  # failure ID -> {"approver": ..., "at": ...}
st.session_state.setdefault("selected", None)
st.session_state.setdefault("errors", {})  # failure ID -> error name from the last failed triage


# --- Header ---------------------------------------------------------------------

st.markdown(
    '<div class="ns-title">🌙 NightShift</div>'
    '<div class="ns-subtitle">Overnight job triage · Nordwerk Antriebstechnik (pilot)</div>'
    '<span class="ns-demo">Demo environment · synthetic data</span>',
    unsafe_allow_html=True,
)

if not api_is_up():
    st.error(
        "**NightShift service is not reachable.**\n\n"
        f"The user interface could not reach the triage service at `{API}`. "
        "Start it with `make api` (and `make infra-up` for the knowledge base and tracing), then reload this page.",
        icon=":material/cloud_off:",
    )
    st.stop()

failures = load_failures()
by_id = {f.id: f for f in failures}
st.session_state.selected = st.session_state.selected or failures[0].id
results: dict[str, tuple[TriageResult, float]] = st.session_state.results
approvals: dict[str, dict] = st.session_state.approvals


# --- Sidebar: demo controls -----------------------------------------------------

with st.sidebar:
    with st.expander("Demo controls", icon=":material/science:", expanded=False):
        st.caption("Simulate outages to show how NightShift degrades safely.")
        # Read the flags from the API on every run, so the toggles always show the truth
        # (the API keeps them in memory, so a restart resets them).
        current = requests.get(f"{API}/admin/chaos", timeout=5).json()
        for flag in CHAOS_FLAGS:
            st.session_state[f"chaos_{flag}"] = current.get(flag, False)
        for flag, label in CHAOS_FLAGS.items():
            st.toggle(label, key=f"chaos_{flag}", on_change=push_chaos, args=(flag,))
        st.button("Reset demo", icon=":material/restart_alt:", on_click=reset_demo, use_container_width=True,
                  help="Turn all simulated outages off and clear the results and approvals.")


# --- Summary strip --------------------------------------------------------------

counts = summary_counts([f.id for f in failures], {k: v[0] for k, v in results.items()}, approvals)
for column, (label, value) in zip(st.columns(4), [
    ("Failures last night", counts["failures"]),
    ("Triaged", counts["triaged"]),
    ("Awaiting approval", counts["awaiting_approval"]),
    ("Escalated to L2", counts["escalated"]),
]):
    column.metric(label, value, border=True)


# --- Approval dialog ------------------------------------------------------------


@st.dialog("Request approval", icon=":material/verified_user:")
def approval_dialog(failure: Failure, result: TriageResult) -> None:
    st.markdown(f"**{failure.job}** · {failure.plant}")
    st.markdown("The recommended steps include actions that need approval from the job owner before they run:")
    for step in result.steps:
        if step.action in ("rerun_job", "config_change"):
            st.markdown(f"- {ACTION_ICONS[step.action]} {step.text}")
    approver = st.text_input("Approver", placeholder="Job owner or deputy")
    confirmed = st.checkbox("The approver has reviewed the diagnosis and the steps.")
    st.caption("The approver's name stays in this browser session. It is not sent to NightShift's logs or traces.")
    if st.button("Record approval", type="primary", disabled=not (approver.strip() and confirmed), use_container_width=True):
        approvals[failure.id] = {"approver": approver.strip(), "at": datetime.now().strftime("%H:%M")}
        st.rerun()


# --- Triage card ----------------------------------------------------------------


def render_result(failure: Failure, result: TriageResult, seconds: float) -> None:
    for banner in banners(result):
        BANNER[banner.kind](banner.text, icon=banner.icon)

    category = CATEGORY_TITLES.get(result.category, result.category)
    st.markdown(f'<div class="ns-root">{result.root_cause}</div>', unsafe_allow_html=True)
    st.markdown(f"{risk_badge(result.risk)} &nbsp; :blue-badge[{category}]")
    st.progress(result.confidence, text=f"Confidence {round(result.confidence * 100)}%")
    if result.escalate and result.escalation_reason:
        with st.expander("Why NightShift is not sure", icon=":material/help:"):
            st.write(result.escalation_reason)

    main, side = st.columns([3, 2], gap="large")
    with main:
        if result.evidence:
            st.markdown('<div class="ns-section">Why we think so</div>', unsafe_allow_html=True)
            st.code("\n".join(result.evidence), language=None, wrap_lines=True)

        st.markdown('<div class="ns-section">Recommended steps</div>', unsafe_allow_html=True)
        for number, step in enumerate(result.steps, start=1):
            st.markdown(f"{number}. {ACTION_ICONS[step.action]} **{ACTION_LABELS[step.action]}** · {step.text}")

        if result.citations:
            st.markdown('<div class="ns-section">Sources</div>', unsafe_allow_html=True)
            for doc_id, (title, excerpts) in group_citations(result).items():
                is_ticket = doc_id.startswith("INC")
                kind, icon = ("Past ticket", ":material/confirmation_number:") if is_ticket else ("Runbook", ":material/menu_book:")
                label = title.removeprefix(f"{doc_id}: ")
                with st.expander(f"{kind} {doc_id} · {label}", icon=icon):
                    for excerpt in excerpts:
                        st.markdown(f"> {excerpt}")

    with side:
        with st.container(border=True):
            st.markdown('<div class="ns-section" style="margin-top:0">Risk and approval</div>', unsafe_allow_html=True)
            for line in risk_explanation(result):
                st.markdown(line)
            if result.approval_required and not result.escalate:
                if failure.id in approvals:
                    approval = approvals[failure.id]
                    st.success(f"Approval recorded by {approval['approver']} at {approval['at']}; "
                               "rerun would be submitted to the scheduler.", icon=":material/task_alt:")
                    st.caption("Demo only: nothing is executed.")
                elif st.button("Request approval", type="primary", icon=":material/verified_user:", use_container_width=True):
                    approval_dialog(failure, result)

    st.divider()
    footer, link = st.columns([4, 1], vertical_alignment="center")
    footer.caption(f"Model: {result.model_used} · Response time: {seconds:.1f} s · "
                   f"Mode: {'full' if result.mode == 'full' else 'degraded'} · Trace: {result.trace_id[:12]}…")
    link.link_button("View trace", phoenix_trace_url(result.trace_id, SETTINGS), icon=":material/timeline:", use_container_width=True)


# --- Layout: failure list and triage card ------------------------------------------

left, right = st.columns([1.15, 2.6], gap="large")

with left:
    st.markdown('<div class="ns-section" style="margin-top:0.4rem">Last night\'s failures</div>', unsafe_allow_html=True)
    selected = st.session_state.selected
    # Highlight the selected card (keyed containers get a `st-key-<key>` CSS class).
    st.markdown(f"<style>.st-key-card-{selected} {{ border-color: #1D4ED8 !important; background: #EFF4FF; "
                "box-shadow: inset 4px 0 0 #1D4ED8; }}</style>", unsafe_allow_html=True)
    with st.container(height=760, border=False):
        for failure in failures:
            result = results.get(failure.id, (None, 0))[0]
            with st.container(border=True, key=f"card-{failure.id}"):
                pin = ":material/push_pin: " if failure.pinned else ""
                st.markdown(f'<div class="ns-job">{failure.job}</div>'
                            f'<div class="ns-meta">{failure.plant} · {failure.system} · failed {clock_time(failure.failed_at)}</div>',
                            unsafe_allow_html=True)
                badge_col, button_col = st.columns([3, 2], vertical_alignment="center")
                badge_col.markdown(f"{pin}{card_badge(result)}")
                if button_col.button("Open", key=f"open-{failure.id}", type="tertiary", icon=":material/arrow_forward:",
                                     disabled=failure.id == selected):
                    st.session_state.selected = failure.id
                    st.rerun()

with right:
    failure = by_id[st.session_state.selected]
    title, action = st.columns([3, 1], vertical_alignment="bottom")
    title.markdown(f'<div class="ns-heading">{failure.job}</div>'
                   f'<div class="ns-meta">{failure.plant} · {failure.system} · failed {clock_time(failure.failed_at)} · '
                   f'ID {failure.id}</div>', unsafe_allow_html=True)
    triaged = failure.id in results
    if action.button("Re-run triage" if triaged else "Run triage", type="secondary" if triaged else "primary",
                     icon=":material/autorenew:" if triaged else ":material/play_arrow:", use_container_width=True):
        with st.spinner("Analysing the log, checking sources and applying safety rules…"):
            try:
                results[failure.id] = run_triage(failure.id)
                approvals.pop(failure.id, None)
                st.session_state.errors.pop(failure.id, None)
            except requests.RequestException as error:
                st.session_state.errors[failure.id] = type(error).__name__
        st.rerun()

    if failure.id in st.session_state.errors:
        st.error(f"**Triage could not be completed.** The service did not answer in time or returned an error "
                 f"({st.session_state.errors[failure.id]}). Try again, or check the API logs.", icon=":material/error:")

    with st.expander("Original job log", icon=":material/description:"):
        st.caption("Shown to operators only. Personal data is removed before anything is sent to an AI model.")
        st.code(load_failure_detail(failure.id).log_text, language=None, wrap_lines=True)

    if failure.id in results:
        render_result(failure, *results[failure.id])
    else:
        st.info("Not triaged yet. Select **Run triage** to diagnose this failure.", icon=":material/pending:")
