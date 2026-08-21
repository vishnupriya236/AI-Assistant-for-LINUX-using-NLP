"""
app.py

Main Flask backend. Implements the full pipeline from spec:

User -> Natural Language -> Intent Detection -> Context Collection ->
LLM Analysis -> Structured Action -> Safety Validation -> Command +
Explanation + Risk -> User Confirmation -> Validated Execution ->
Actual Output -> (LLM Error Analysis if needed) -> Result shown to user

Intent detection here is a lightweight keyword router that decides WHICH
real context to collect (disk vs process vs service vs file vs log) — it
does not try to replace the LLM for understanding nuance. The LLM does the
actual reasoning/explanation over the real, already-collected data. This
keeps context collection bounded and safe (we never let the LLM decide
which files/logs to read) while still using the LLM for language
understanding and explanation, per spec section 6.
"""

import os
import re
import traceback
from dotenv import load_dotenv
load_dotenv()  # must run before ai.llm_service reads GEMINI_API_KEY from the environment

from flask import Flask, request, jsonify, render_template

from context.system_info import get_full_system_context
from context.disk_info import get_partitions, get_root_disk_summary, find_largest_directories, find_largest_files
from context.process_info import get_all_processes, get_my_processes, get_process_by_pid
from context.file_info import analyze_path
from logs.log_analyzer import get_service_status, get_service_logs, get_recent_errors

from ai.llm_service import call_llm, LLMResponseError
from ai.prompts import build_analysis_prompt, build_error_analysis_prompt

from security.command_validator import validate_command
from execution.command_executor import execute_command

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Lightweight intent detection -> decides what REAL context to collect.
# This is deliberately simple keyword routing, not a hardcoded response
# generator — the actual explanation still comes from the LLM over real data.
# ---------------------------------------------------------------------------

def detect_intent_and_collect_context(user_query: str) -> dict:
    q = user_query.lower()
    context = {"system": get_full_system_context()}

    # File / permission questions: extract a quoted or bare filename-looking token
    file_match = re.search(r'([./~][\w./\-]+|\b[\w\-]+\.\w{1,5}\b)', user_query)
    is_file_question = any(kw in q for kw in ["file", "access", "permission", "can't open", "cannot open"])

    if is_file_question and file_match:
        context["file_analysis"] = analyze_path(file_match.group(1))

    if any(kw in q for kw in ["disk", "storage", "space", "full"]):
        context["disk"] = {
            "partitions": get_partitions(),
            "root_summary": get_root_disk_summary(),
        }
        if any(kw in q for kw in ["large", "biggest", "what's taking", "taking up"]):
            context["disk"]["largest_directories"] = find_largest_directories("/", top_n=10)

    if any(kw in q for kw in ["cpu", "processor"]):
        context["cpu_detail"] = get_all_processes(sort_by="cpu", limit=10)

    if any(kw in q for kw in ["ram", "memory"]):
        context["memory_detail"] = get_all_processes(sort_by="memory", limit=10)

    if any(kw in q for kw in ["process", "running", "consuming"]):
        context["processes"] = get_all_processes(sort_by="cpu", limit=15)
        if "my" in q or "belonging to" in q:
            context["my_processes"] = get_my_processes(limit=15)

    # Service / log questions: try to extract a service name
    service_match = re.search(r'\b(ssh|nginx|apache2?|mysql|postgres(?:ql)?|docker|cron|network[- ]?manager)\b', q)
    if any(kw in q for kw in ["service", "ssh", "daemon", "fail", "not working", "stopped", "crash"]):
        service_name = service_match.group(1).replace(" ", "-") if service_match else "ssh"
        context["service_status"] = get_service_status(service_name)
        context["service_logs"] = get_service_logs(service_name, lines=40)

    if any(kw in q for kw in ["log", "error", "why did"]) and "service_logs" not in context:
        context["recent_errors"] = get_recent_errors(lines=40)

    return context


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/context", methods=["GET"])
def api_context():
    """Raw real system context, for the UI's context panel."""
    return jsonify(get_full_system_context())


@app.route("/api/query", methods=["POST"])
def api_query():
    """
    Main pipeline entry point: natural language in, structured
    analysis/action out. Does NOT execute anything.
    """
    body = request.get_json(silent=True) or {}
    user_query = (body.get("query") or "").strip()
    if not user_query:
        return jsonify({"error": "Missing 'query' in request body."}), 400

    try:
        collected_context = detect_intent_and_collect_context(user_query)
    except Exception as e:
        app.logger.error(f"Context collection failed: {e}\n{traceback.format_exc()}")
        return jsonify({"error": f"Failed to collect real system context: {e}"}), 500

    prompt = build_analysis_prompt(user_query, collected_context.get("system", {}),
                                    extra_context={k: v for k, v in collected_context.items() if k != "system"})

    try:
        llm_result = call_llm(prompt)
    except LLMResponseError as e:
        return jsonify({
            "error": str(e),
            "context_collected": collected_context,
        }), 502

    # Re-validate the LLM's proposed command independently — never trust its
    # self-reported risk_level.
    backend_validation = None
    if llm_result.get("command"):
        v = validate_command(llm_result["command"])
        backend_validation = v.to_dict()
        # Backend risk assessment always wins over the LLM's own claim.
        llm_result["risk_level"] = backend_validation["risk_level"]
        llm_result["requires_confirmation"] = backend_validation["requires_confirmation"]
        if not backend_validation["allowed"]:
            llm_result["action"] = "analyze"
            llm_result["explanation"] += (
                f" (Note: a proposed command was blocked by the safety layer: "
                f"{backend_validation['reason']})"
            )
            llm_result["command"] = None

    return jsonify({
        "llm_result": llm_result,
        "backend_validation": backend_validation,
        "context_collected": collected_context,
    })


@app.route("/api/execute", methods=["POST"])
def api_execute():
    """
    Executes a command ONLY if:
      - it independently passes security validation, AND
      - the user has explicitly confirmed (or it's LOW risk and confirmation
        isn't required).
    This is the only route in the app that touches subprocess.
    """
    body = request.get_json(silent=True) or {}
    command = (body.get("command") or "").strip()
    user_confirmed = bool(body.get("confirmed", False))

    if not command:
        return jsonify({"error": "Missing 'command' in request body."}), 400

    result = execute_command(command, user_confirmed=user_confirmed)
    result_dict = result.to_dict()

    # If it failed to execute for a real reason (not a safety block), or
    # ran but returned a non-zero exit code, ask the LLM to explain the
    # actual error using the real stderr/return code.
    needs_error_explanation = (
        (result.executed and result.return_code != 0) or
        (not result.executed and result.block_reason and "safety layer" not in (result.block_reason or "").lower()
         and "Confirmation required" not in (result.block_reason or ""))
    )

    if needs_error_explanation:
        try:
            prompt = build_error_analysis_prompt(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.return_code if result.executed else -1,
                system_context=get_full_system_context(),
            )
            error_analysis = call_llm(prompt)
            result_dict["ai_error_analysis"] = error_analysis
        except LLMResponseError as e:
            result_dict["ai_error_analysis"] = {"error": str(e)}

    return jsonify(result_dict)


@app.route("/api/process/<int:pid>", methods=["GET"])
def api_process_detail(pid):
    """Real detail for a single process — used before confirming a kill."""
    return jsonify(get_process_by_pid(pid))


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug_mode)
