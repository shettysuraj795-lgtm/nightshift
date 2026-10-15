# NightShift: guidance for Claude Code

The full build plan is in [docs/BUILD_PLAN_MVP.md](docs/BUILD_PLAN_MVP.md). Build one phase at a time, exactly as specified there. Keep the schemas, file names and folder layout as written, because this MVP is a strict subset of a larger master plan and later expansion must be additive, not a rewrite.

## Project context

**What we are building:** NightShift, an AI triage assistant for failed overnight batch jobs. An operator picks a failed job; NightShift returns a structured diagnosis (root cause, category, confidence, fix steps, cited sources) and a **risk-gated recommendation**. Every step is traced, and an eval suite scores it against known ground truth.

**Why:** A demo for a role-played technical deep dive with a fictional client, **Nordwerk Antriebstechnik GmbH** (German manufacturer; SAP S/4HANA, plant MES, Azure data platform; ~2,800 batch jobs a night; L1 operators triage manually, ~40% escalate to L2). It will later become a GitHub portfolio piece.

**The demo must answer three client objections:**
1. **Trust:** "If it tells an operator to rerun a posting job halfway through, we get duplicate postings." → deterministic risk policy, citations, abstention.
2. **Security:** "What leaves our network, and what if someone plants instructions in a log?" → PII redaction, injection defence.
3. **Assurance:** "How do we know it is good, and stays good?" → tracing, evals, a regression gate.

Plus: graceful degradation when a model or component fails.

**Everything is synthetic.** No real company data, no copied vendor documentation. The vendor is a role-played Aleph Alpha; nothing should claim to be a real Aleph Alpha product or feature.

## Working rules

- **Explainability over cleverness.** The author must be able to explain every line. Plain Python, small modules, docstrings. **No LangChain, LangGraph or agent frameworks.**
- **Phase by phase.** Plan, then build only the current phase. At the end print: what was built, exact commands to verify, known limitations. Then stop.
- **Tests:** `pytest` for deterministic logic only (registry consistency, policy, redaction, citation validation, signature matcher, fallback with mocked providers).
- **Config, not constants:** model IDs, thresholds, timeouts, chaos flags in `.env` / YAML.
- **Secrets** only in `.env` (git-ignored); provide `.env.example`.
- **Reproducible:** seed 42; generated data is committed.
- **British English** in UI, docs and comments.
- One conventional commit per phase. Pin versions. Use `uv`.

## Rules for subagents

- Follow this file.
- Never change `app/schemas.py` or the registry format. If a schema change is genuinely needed, stop and tell the main session.
- Report what you changed.
