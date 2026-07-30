# XAU Analysis Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a premium local browser console that starts a read-only Bitget MT5 XAUUSD analysis job, exposes durable stage progress, and displays only fact-gated Qwen research.

**Architecture:** Add an isolated `local_console` sidecar beside the upstream TradingAgents package. A standard-library localhost server serves one static page and a small JSON API. The sidecar invokes the existing read-only MT5 snapshot script with a job-specific output file, evaluates deterministic guards, uses TradingAgents' Qwen client only after the guard permits it, and persists each job as local JSON.

**Tech Stack:** Python 3.12, standard library `http.server` and `concurrent.futures`, existing `python-dotenv`, existing TradingAgents Qwen client, static HTML/CSS/JavaScript, pytest. No new dependency.

## Global Constraints

- Bind only to `127.0.0.1:8765`.
- Reuse `D:\XAU\scripts\mt5_xau_market_context_once.py` as the sole live price source.
- Never call order, position, close, modify, SL/TP, login, account-setting or MT5 configuration APIs.
- Runtime data belongs in `data_cache/xau_analysis_console/` and is ignored by Git.
- Read Qwen credentials only from the existing local `.env`, never send them to the browser or write them to reports.
- Use `qwen3.7-max` for a normal brief and `qwen3.8-max-preview` only for a user-started deep review.
- Do not show a model report in the decision panel unless the fact gate accepts it.

---

## File Structure

- Create: `local_console/__init__.py`, package marker.
- Create: `local_console/config.py`, repository and local-runtime configuration.
- Create: `local_console/jobs.py`, durable job records and legal stage transitions.
- Create: `local_console/snapshot.py`, subprocess snapshot capture and JSONL parsing.
- Create: `local_console/guard.py`, snapshot and event-context gate.
- Create: `local_console/brief.py`, constrained Qwen prompt and report validation.
- Create: `local_console/service.py`, background job orchestration.
- Create: `local_console/server.py`, localhost HTTP API and static-file serving.
- Create: `local_console/static/index.html`, `local_console/static/app.css`, `local_console/static/app.js`, desktop control surface.
- Create: `scripts/run_xau_analysis_console.py`, one-command local launcher.
- Create: `tests/local_console/__init__.py`, `test_jobs.py`, `test_guard.py`, `test_service.py`, `test_server.py`, `test_static.py`.
- Modify: `.gitignore`, ignore `data_cache/xau_analysis_console/`.

## Shared Interfaces

```python
JobKind = Literal["brief", "deep_review"]
JobStage = Literal[
    "QUEUED", "SNAPSHOT", "GATE", "MODEL", "VALIDATE",
    "COMPLETE", "REJECTED", "FAILED",
]

@dataclass
class JobRecord:
    id: str
    kind: JobKind
    stage: JobStage
    created_at: str
    updated_at: str
    detail: str
    elapsed_seconds: float
    snapshot: dict[str, object] | None
    gate: dict[str, object] | None
    report: dict[str, object] | None

def create_job(kind: JobKind) -> JobRecord: ...
def transition(job_id: str, stage: JobStage, detail: str, **updates: object) -> JobRecord: ...
def capture_snapshot(config: ConsoleConfig, job_id: str) -> dict[str, object]: ...
def evaluate_gate(snapshot: dict[str, object], event_context: dict[str, object], now: datetime) -> GateResult: ...
def validate_report(payload: object, gate: GateResult) -> tuple[bool, str, dict[str, object] | None]: ...
```

### Task 1: Add isolated configuration and durable job records

**Files:**
- Create: `local_console/__init__.py`
- Create: `local_console/config.py`
- Create: `local_console/jobs.py`
- Create: `tests/local_console/__init__.py`
- Create: `tests/local_console/test_jobs.py`
- Modify: `.gitignore`

**Consumes:** Existing repository `.env` and `data_cache/`.

**Produces:** `ConsoleConfig`, `JobStore`, `JobRecord`, and a runtime path that later tasks can use without touching upstream package state.

- [ ] **Step 1: Write failing job-store tests**

```python
def test_job_store_persists_transition_across_instances(tmp_path):
    first = JobStore(tmp_path)
    created = first.create("brief")
    first.transition(created.id, "SNAPSHOT", "Reading MT5 snapshot")

    restored = JobStore(tmp_path).get(created.id)

    assert restored.stage == "SNAPSHOT"
    assert restored.detail == "Reading MT5 snapshot"
    assert restored.elapsed_seconds >= 0


def test_job_store_rejects_terminal_transition(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("brief")
    store.transition(job.id, "COMPLETE", "Done")

    with pytest.raises(ValueError, match="terminal"):
        store.transition(job.id, "MODEL", "Must not restart")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_jobs.py -q`

Expected: FAIL because `local_console.jobs` does not exist.

- [ ] **Step 3: Implement the smallest persistent store**

```python
TERMINAL_STAGES = {"COMPLETE", "REJECTED", "FAILED"}

class JobStore:
    def create(self, kind: JobKind) -> JobRecord:
        record = JobRecord(id=uuid4().hex, kind=kind, stage="QUEUED", ...)
        return self._write(record)

    def transition(self, job_id: str, stage: JobStage, detail: str, **updates: object) -> JobRecord:
        record = self.get(job_id)
        if record.stage in TERMINAL_STAGES:
            raise ValueError("terminal jobs cannot transition")
        record.stage, record.detail, record.updated_at = stage, detail, utc_now()
        for key, value in updates.items():
            setattr(record, key, value)
        return self._write(record)
```

`ConsoleConfig.from_repo()` loads `.env` server-side, validates the existing Qwen URL and model variables without exposing their values, uses `data_cache/xau_analysis_console/` for runtime files, and defaults `XAU_CONSOLE_MT5_PYTHON` to the already installed global Python interpreter. The configuration fails explicitly if that interpreter or the snapshot script is missing.

- [ ] **Step 4: Ignore runtime state and run the tests**

Add this exact line to `.gitignore`:

```gitignore
data_cache/xau_analysis_console/
```

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_jobs.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add local_console tests/local_console .gitignore
git commit -m "feat: add durable console job state"
```

### Task 2: Capture only read-only MT5 facts and apply deterministic guards

**Files:**
- Create: `local_console/snapshot.py`
- Create: `local_console/guard.py`
- Create: `tests/local_console/test_guard.py`

**Consumes:** `ConsoleConfig`, `JobStore`, and the existing MT5 snapshot script.

**Produces:** A normalized job snapshot and one of `ANALYSE`, `WATCH`, `WAIT`, or `BLOCKED`.

- [ ] **Step 1: Write failing guard tests**

```python
def test_stale_snapshot_is_blocked():
    snapshot = {"timestamp": "2026-07-30T00:00:00+00:00", "identity_match": True,
                "bid": 4000.0, "ask": 4000.1, "symbol": "XAUUSD"}

    result = evaluate_gate(snapshot, {"status": "verified_clear"},
                           datetime(2026, 7, 30, 0, 2, tzinfo=UTC))

    assert result.action == "BLOCKED"
    assert result.reason == "MT5 snapshot is older than 60 seconds"


def test_unverified_events_force_watch_not_model_block():
    result = evaluate_gate(fresh_snapshot(), {"status": "unverified"}, utc_now())

    assert result.action == "WATCH"
    assert result.allow_model is True
    assert result.directional_plan_allowed is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_guard.py -q`

Expected: FAIL because `evaluate_gate` does not exist.

- [ ] **Step 3: Implement snapshot capture and gate**

```python
def capture_snapshot(config: ConsoleConfig, job_id: str) -> dict[str, object]:
    output = config.snapshots_dir / f"{job_id}.jsonl"
    command = [str(config.mt5_python), str(config.mt5_snapshot_script),
               "--symbol", "XAUUSD", "--output", str(output)]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
    return json.loads(output.read_text(encoding="utf-8").strip().splitlines()[-1])

def evaluate_gate(snapshot: dict[str, object], event_context: dict[str, object], now: datetime) -> GateResult:
    if snapshot.get("identity_match") is not True or snapshot.get("symbol") != "XAUUSD":
        return GateResult("BLOCKED", False, False, "MT5 broker or symbol identity mismatch")
    if not valid_quote(snapshot):
        return GateResult("BLOCKED", False, False, "MT5 quote is unavailable")
    if snapshot_age_seconds(snapshot, now) > 60:
        return GateResult("BLOCKED", False, False, "MT5 snapshot is older than 60 seconds")
    if event_context.get("status") == "wait":
        return GateResult("WAIT", False, False, str(event_context["reason"]))
    if event_context.get("status") != "verified_clear":
        return GateResult("WATCH", True, False, "Event context is unverified")
    return GateResult("ANALYSE", True, True, "Fresh MT5 snapshot and verified event context")
```

`load_event_context()` reads only `data_cache/xau_analysis_console/event_context.json`. When absent or malformed it returns `{"status": "unverified"}`. It does not invent events or fetch an untrusted calendar.

- [ ] **Step 4: Run focused tests and a one-time mutation scan**

Run:

```powershell
D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_guard.py -q
rg -n -i "order_send|positions_get|order_modify|order_close|sl|tp|login\(" local_console scripts/run_xau_analysis_console.py
```

Expected: guard tests PASS. The scan has no order or position mutation calls. `sl` and `tp` may appear only in the test search pattern itself, not in source files.

- [ ] **Step 5: Commit**

```powershell
git add local_console tests/local_console
git commit -m "feat: gate console analysis on fresh MT5 facts"
```

### Task 3: Add constrained Qwen research and report validation

**Files:**
- Create: `local_console/brief.py`
- Create: `tests/local_console/test_service.py`

**Consumes:** `GateResult`, normalized MT5 snapshot, `create_llm_client()` from `tradingagents.llm_clients.factory`.

**Produces:** Structured accepted research or a terminal `REJECTED` job with no raw model advice in the decision surface.

- [ ] **Step 1: Write failing report-validator tests**

```python
def test_report_with_unprovided_source_is_rejected():
    allowed = GateResult("ANALYSE", True, True, "ok")
    payload = {"action": "ANALYSE", "source_ids": ["mt5_snapshot", "Yahoo Finance"],
               "summary": "...", "invalidation": "...", "next_observation": "..."}

    accepted, reason, report = validate_report(payload, allowed)

    assert accepted is False
    assert reason == "report cites an unprovided source: Yahoo Finance"
    assert report is None


def test_watch_report_cannot_contain_entry_instruction():
    payload = {"action": "WATCH", "source_ids": ["mt5_snapshot"],
               "summary": "Buy now", "invalidation": "...", "next_observation": "..."}

    accepted, reason, _ = validate_report(payload, GateResult("WATCH", True, False, "events unknown"))

    assert accepted is False
    assert reason == "WATCH report contains a direct entry instruction"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_service.py -q`

Expected: FAIL because `local_console.brief` does not exist.

- [ ] **Step 3: Implement the constrained adapter**

```python
def request_brief(config: ConsoleConfig, kind: JobKind, snapshot: dict[str, object], gate: GateResult) -> object:
    model = config.deep_model if kind == "deep_review" else config.quick_model
    llm = create_llm_client("qwen", model, config.backend_url).get_llm()
    response = llm.invoke(build_prompt(snapshot, gate, kind))
    return json.loads(str(response.content))

def build_prompt(snapshot: dict[str, object], gate: GateResult, kind: JobKind) -> str:
    return json.dumps({
        "role": "XAU manual analysis assistant",
        "allowed_sources": ["mt5_snapshot", "verified_event_context"],
        "gate_action": gate.action,
        "directional_plan_allowed": gate.directional_plan_allowed,
        "facts": snapshot,
        "required_json_keys": ["action", "source_ids", "summary", "invalidation", "next_observation"],
        "prohibitions": ["unprovided sources", "guaranteed returns", "orders", "event-window entries"],
    }, ensure_ascii=False)
```

The validator accepts only object-shaped JSON with the five required keys, source IDs from the prompt, and no direct entry wording when the gate is `WATCH`. It never stores or returns an invalid raw response to the browser.

- [ ] **Step 4: Run focused tests with a fake LLM callable**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_service.py -q`

Expected: PASS without network access or a Qwen key.

- [ ] **Step 5: Commit**

```powershell
git add local_console tests/local_console
git commit -m "feat: add fact-gated Qwen analysis"
```

### Task 4: Orchestrate durable background jobs and JSON endpoints

**Files:**
- Create: `local_console/service.py`
- Create: `local_console/server.py`
- Create: `tests/local_console/test_server.py`

**Consumes:** Job store, snapshot capture, gate, constrained Qwen adapter.

**Produces:** A local job API that immediately returns a job ID and lets the browser poll durable stages.

- [ ] **Step 1: Write failing service and API tests**

```python
def test_start_job_returns_queued_job_before_worker_finishes(tmp_path):
    service = ConsoleService.for_test(tmp_path, snapshot_runner=slow_fake_snapshot, brief_runner=fake_brief)

    job = service.start("brief")

    assert job.stage == "QUEUED"
    assert service.store.get(job.id).stage in {"QUEUED", "SNAPSHOT"}


def test_job_endpoint_exposes_stage_and_elapsed_seconds(server_client):
    created = server_client.post_json("/api/jobs", {"kind": "brief"})
    current = server_client.get_json(f"/api/jobs/{created['id']}")

    assert current["stage"] in {"QUEUED", "SNAPSHOT", "GATE", "MODEL", "VALIDATE", "COMPLETE"}
    assert isinstance(current["elapsed_seconds"], float)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_server.py -q`

Expected: FAIL because `ConsoleService` and the HTTP endpoints do not exist.

- [ ] **Step 3: Implement task progression and HTTP contract**

```python
def run_job(self, job_id: str) -> None:
    self.store.transition(job_id, "SNAPSHOT", "Reading MT5 snapshot")
    snapshot = self.snapshot_runner(self.config, job_id)
    self.store.transition(job_id, "GATE", "Checking data freshness and event context", snapshot=snapshot)
    gate = evaluate_gate(snapshot, load_event_context(self.config), utc_now())
    if not gate.allow_model:
        self.store.transition(job_id, "COMPLETE", gate.reason, gate=gate.to_dict())
        return
    self.store.transition(job_id, "MODEL", "Submitting Qwen research", gate=gate.to_dict())
    payload = self.brief_runner(self.config, self.store.get(job_id).kind, snapshot, gate)
    self.store.transition(job_id, "VALIDATE", "Checking report sources and constraints")
    accepted, reason, report = validate_report(payload, gate)
    self.store.transition(job_id, "COMPLETE" if accepted else "REJECTED", reason, report=report)
```

Expose only these endpoints:

```text
GET  /api/status
POST /api/jobs             body: {"kind": "brief" | "deep_review"}
GET  /api/jobs/<job-id>
GET  /api/history
GET  /                       static page
GET  /static/<asset>        static CSS or JavaScript
```

The server uses `ThreadingHTTPServer(("127.0.0.1", 8765), Handler)` and `ThreadPoolExecutor(max_workers=1)`. A single worker prevents overlapping MT5 captures and makes the visible progress sequence truthful.

- [ ] **Step 4: Run focused tests**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_server.py tests/local_console/test_jobs.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add local_console tests/local_console
git commit -m "feat: expose asynchronous analysis jobs"
```

### Task 5: Build the premium desktop control surface

**Files:**
- Create: `local_console/static/index.html`
- Create: `local_console/static/app.css`
- Create: `local_console/static/app.js`
- Create: `tests/local_console/test_static.py`

**Consumes:** `/api/status`, `/api/jobs/<job-id>`, `/api/history`.

**Produces:** A dark, data-first dashboard where a user can start either task and observe every durable stage.

- [ ] **Step 1: Write static-contract tests**

```python
def test_dashboard_has_both_job_buttons_and_progress_region():
    page = Path("local_console/static/index.html").read_text(encoding="utf-8")

    assert 'id="start-brief"' in page
    assert 'id="start-deep-review"' in page
    assert 'id="job-progress"' in page
    assert 'aria-live="polite"' in page


def test_browser_polling_uses_job_api_not_a_fake_timer():
    script = Path("local_console/static/app.js").read_text(encoding="utf-8")

    assert 'fetch(`/api/jobs/${jobId}`)' in script
    assert 'setInterval' in script
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_static.py -q`

Expected: FAIL because static files do not exist.

- [ ] **Step 3: Implement the page and real progress**

The HTML includes a persistent top status bar, bid/ask panel, market-structure cards, event card, one primary brief button, one secondary deep-review button, stage timeline, report panel and history rail. Buttons call `POST /api/jobs`, retain the returned `jobId` in `sessionStorage`, disable only while their own job is active, and begin polling immediately.

```javascript
async function startJob(kind) {
  const job = await postJson('/api/jobs', { kind });
  sessionStorage.setItem('xau-analysis-job-id', job.id);
  renderJob(job);
  pollJob(job.id);
}

function pollJob(jobId) {
  const timer = window.setInterval(async () => {
    const job = await fetch(`/api/jobs/${jobId}`).then((response) => response.json());
    renderJob(job);
    if (["COMPLETE", "REJECTED", "FAILED"].includes(job.stage)) {
      window.clearInterval(timer);
      refreshHistory();
    }
  }, 1000);
}
```

The CSS uses near-black background, restrained teal/amber/red status colors, tabular numerals, a 12-column desktop grid, visible keyboard focus, and `prefers-reduced-motion`. The progress region renders all five stages, completed stages, the active stage, start timestamp, elapsed time and exact terminal reason. It does not render buy/sell controls.

- [ ] **Step 4: Run static tests and manually verify layout**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_static.py -q`

Expected: PASS.

Manual check at 1440px: header, event state, action state, both task controls and progress timeline are visible without scrolling.

- [ ] **Step 5: Commit**

```powershell
git add local_console/static tests/local_console/test_static.py
git commit -m "feat: add XAU analysis control surface"
```

### Task 6: Add launcher, full verification and local use instructions

**Files:**
- Create: `scripts/run_xau_analysis_console.py`
- Create: `docs/xau-analysis-console.md`
- Modify: `tests/local_console/test_server.py`

**Consumes:** The completed server and static page.

**Produces:** A single local startup command and evidence that the console has no trading mutation path.

- [ ] **Step 1: Write the launcher test**

```python
def test_launcher_builds_localhost_command(monkeypatch):
    monkeypatch.setattr(run_xau_analysis_console, "open_browser", lambda url: None)

    host, port = run_xau_analysis_console.launch_arguments([])

    assert (host, port) == ("127.0.0.1", 8765)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console/test_server.py::test_launcher_builds_localhost_command -q`

Expected: FAIL because the launcher does not exist.

- [ ] **Step 3: Implement launcher and usage document**

```python
def launch_arguments(argv: list[str]) -> tuple[str, int]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        raise SystemExit("XAU Analysis Console only serves 127.0.0.1")
    return args.host, args.port
```

Document this exact launch command:

```powershell
D:\XAU\TradingAgents\.venv\Scripts\python.exe D:\XAU\TradingAgents\scripts\run_xau_analysis_console.py
```

The document explains the two page buttons, all job stages, why `WATCH`, `WAIT`, `BLOCKED` and `REJECTED` are valid outcomes, and that no page action sends any trading instruction to MT5.

- [ ] **Step 4: Run full tests, lint, local server check and mutation scan**

Run:

```powershell
D:\XAU\TradingAgents\.venv\Scripts\python.exe -m pytest tests/local_console -q
D:\XAU\TradingAgents\.venv\Scripts\python.exe -m ruff check local_console scripts/run_xau_analysis_console.py tests/local_console
D:\XAU\TradingAgents\.venv\Scripts\python.exe D:\XAU\TradingAgents\scripts\run_xau_analysis_console.py --help
rg -n -i "order_send|positions_get|order_modify|order_close|trade\.buy|trade\.sell|enable.*trading" local_console scripts/run_xau_analysis_console.py
```

Expected: tests and Ruff PASS, launcher prints help, mutation scan has no matches.

- [ ] **Step 5: Commit**

```powershell
git add scripts/run_xau_analysis_console.py docs/xau-analysis-console.md tests/local_console
git commit -m "docs: add local XAU console launcher"
```

## Plan Self-Review

- Spec coverage: Tasks 1 to 6 cover local-only hosting, MT5 snapshot facts, event state, Qwen real-time and deep jobs, visible durable progress, report rejection, history, visual hierarchy, documentation and mutation verification.
- Dependency check: FastAPI, Flask and Uvicorn are not installed. The plan uses only the standard library plus existing project dependencies.
- Scope check: Official event-feed ingestion is deliberately excluded from this slice. The interface reports `unverified` rather than fabricating a clear event state. This keeps the first usable console self-contained.
- Type check: `JobRecord`, `JobStore`, `ConsoleConfig`, `GateResult`, `capture_snapshot`, `evaluate_gate` and `validate_report` are defined before their later use.
- Placeholder scan: no deferred implementation markers are present.
