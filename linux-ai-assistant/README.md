# linux-ai-assistant

A context-aware AI assistant for Linux system administration. You ask questions
in plain English ("why is my disk almost full?"), it collects **real** data from
the machine it's running on, sends that real data to an LLM for explanation,
and — if a fix requires running a command — shows you exactly what it wants to
run, how risky it is, and waits for your explicit confirmation before touching
anything.

Nothing in this project is simulated. There is no fake filesystem, no invented
CPU/RAM numbers, no pretend command output. Every number and every command
result comes from the actual Linux machine running the app.

## How it's built (status)

Phases 1–6 and 8–13 from the project plan are implemented and have been
smoke-tested against a real Linux container while building this:

| Phase | What | Status |
|---|---|---|
| 1–2 | Ubuntu/WSL2 + Flask app | ✅ `app.py` |
| 3 | Real system-context collection | ✅ `context/system_info.py` |
| 4 | CPU/RAM/disk/process monitoring | ✅ `context/disk_info.py`, `context/process_info.py` |
| 5 | File/permission analysis | ✅ `context/file_info.py` |
| 6 | Log retrieval (journalctl, scoped) | ✅ `logs/log_analyzer.py` |
| 7 | Gemini integration | ✅ `ai/llm_service.py` (needs your API key — see below) |
| 8 | Structured intent/action JSON | ✅ enforced schema in `ai/prompts.py` |
| 9 | Explanation + recommendation | ✅ part of the structured LLM response |
| 10 | Command validation + risk classification | ✅ `security/risk_classifier.py`, `security/command_validator.py` |
| 11 | User confirmation | ✅ frontend confirm card + backend `confirmed` flag |
| 12 | Controlled execution | ✅ `execution/command_executor.py` — real subprocess, no `shell=True` |
| 13 | Error analysis | ✅ `/api/execute` re-prompts the LLM with real stderr/return code |
| 14–15 | UI polish + full test pass | You're here — see "What to do next" below |

## Architecture

```
Browser (chat UI)
   │
   ▼
Flask (app.py)
   │
   ├─ detect_intent_and_collect_context()   ← keyword routing, decides WHAT
   │                                           real data to fetch (bounded,
   │                                           backend-controlled — the LLM
   │                                           never picks which files/logs
   │                                           to read)
   │
   ├─ context/*.py                          ← real psutil/os/stat calls
   ├─ logs/log_analyzer.py                  ← real journalctl, scoped
   │
   ├─ ai/llm_service.py                     ← Gemini call, JSON-only,
   │                                           schema-validated response
   │
   ├─ security/risk_classifier.py           ← deterministic risk tier,
   │     security/command_validator.py         backend NEVER trusts the
   │                                           LLM's self-reported risk
   │
   ├─ (frontend shows command + risk +
   │   effect, waits for explicit click)
   │
   └─ execution/command_executor.py         ← the ONLY place subprocess.run
                                                is called; shell=False always;
                                                CRITICAL commands can never be
                                                executed even if "confirmed"
```

The LLM only ever sees: your question, plus real data the backend already
collected. It only ever produces: a JSON object with an explanation and
*optionally* one proposed command. It never gets shell access — the backend
independently re-validates and re-classifies any command it proposes before
anything is shown to you, let alone run.

## Setup

**1. Get the code running (Phase 1–2)**

```bash
# Ubuntu, or Ubuntu via WSL2 on Windows
cd linux-ai-assistant
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Add your Gemini API key (Phase 7)**

```bash
cp .env.example .env
# edit .env and paste your key from https://aistudio.google.com/apikey
```

**3. Run it**

```bash
python3 app.py
```

Open **http://127.0.0.1:5000**. The left panel shows live CPU/RAM/host info
pulled straight from the machine you're running this on — refreshes every 8s.

**4. Try the three demo scenarios from the spec**

- `"Why is my disk almost full?"` — collects real partition + largest-directory
  data, LLM explains it, no confirmation needed (read-only).
- `"Why is my SSH service not working?"` — collects real `systemctl status` +
  recent `journalctl` lines for `ssh`, LLM explains + may propose a restart
  command, which requires your confirmation because it's HIGH risk.
- `"Delete <some file>"` — LLM proposes an `rm` command, you see it, its risk
  level, and must explicitly click Confirm before it runs for real.

## Testing without a Linux box handy

Everything under `context/`, `logs/`, `security/`, and `execution/` can be run
standalone (each file has a `__main__` block) to sanity-check against whatever
Linux environment you're on:

```bash
export PYTHONPATH=.
python3 context/system_info.py
python3 context/disk_info.py
python3 security/risk_classifier.py
python3 execution/command_executor.py
```

Note: `logs/log_analyzer.py` requires `systemctl`/`journalctl` (systemd) to
return live service data — on a systemd-less environment it will honestly
report that instead of faking output, per the "never fake data" rule.

## What to do next (Phase 14–15)

The MVP pipeline is complete and wired end-to-end. Natural next steps, in
spec order:

1. **Run the three demo scenarios on your actual Ubuntu/WSL2 box** and note
   anywhere the keyword-based intent router in `detect_intent_and_collect_context()`
   misses a phrasing you care about — it's intentionally simple and easy to extend.
2. **Add more service/log patterns** to the regex in `app.py` if you want the
   assistant to recognize services beyond ssh/nginx/mysql/postgres/docker/cron
   out of the box.
3. **Wire up `/api/process/<pid>` in the frontend** for the "which process is
   using the most CPU → stop it" flow (backend route already exists and is
   tested; frontend button not yet built).
4. **Run through the full test list in spec §26** (ambiguous requests, denied
   permissions, LLM failures, user cancellation) — the clarify-intent path and
   error-analysis path are both implemented but worth stress-testing with your
   real Gemini key.
5. Only after that: consider Phase 24 scalability items (Ollama local LLM
   option, command history, network/package-management modules) — deliberately
   left out of the MVP per spec §24–25.

## Safety notes (read this before demoing)

- `security/risk_classifier.py` and `security/command_validator.py` are the
  actual gate — not the LLM. The LLM's `risk_level` field is advisory only;
  the backend always re-derives it and overwrites it.
- Command chaining (`&&`, `;`, `|` except a narrow read-only-pipe exception,
  `` ` ``, `$(...)`, `>`) is rejected outright — every command the system
  proposes or runs is a single, unchained invocation.
- `sudo` is never invoked automatically. It only appears in commands you
  explicitly confirmed, and only for actions (like restarting a system
  service) that need it.
- A small set of destructive patterns (`rm -rf /`, `dd` to a raw device,
  `mkfs`, fork bombs, `curl | sh`, recursive chmod/chown on `/`) are
  **hard-blocked** — they cannot be executed even with confirmation.
