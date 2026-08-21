"""
ai/llm_service.py

Wraps the Gemini API call. Enforces JSON-only structured responses and
never lets a malformed/unparseable LLM response fall through to execution.
"""

import os
import json
import re

from ai.prompts import SYSTEM_PROMPT

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

ALLOWED_ACTIONS = {"analyze", "execute", "clarify"}
ALLOWED_RISK_LEVELS = {"low", "medium", "high", "critical"}

REQUIRED_FIELDS = {
    "intent", "action", "command", "explanation",
    "recommendation", "risk_level", "requires_confirmation",
}


class LLMResponseError(Exception):
    pass


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _validate_schema(data: dict) -> dict:
    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise LLMResponseError(f"LLM response missing required fields: {missing}")
    if data["action"] not in ALLOWED_ACTIONS:
        raise LLMResponseError(f"Invalid action: {data['action']}")
    if data["risk_level"] not in ALLOWED_RISK_LEVELS:
        raise LLMResponseError(f"Invalid risk_level: {data['risk_level']}")
    data.setdefault("clarifying_question", None)
    return data


def call_llm(prompt: str) -> dict:
    """
    Calls Gemini with the system prompt + user prompt, parses and validates
    the structured JSON response. Raises LLMResponseError on any malformed
    output rather than silently passing bad data downstream.
    """
    if not GEMINI_API_KEY:
        raise LLMResponseError(
            "GEMINI_API_KEY is not set. Add it to your .env file — see .env.example."
        )

    try:
        import google.generativeai as genai
    except ImportError:
        raise LLMResponseError(
            "google-generativeai package not installed. Run: pip install google-generativeai"
        )

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT,
        generation_config={"response_mime_type": "application/json"},
    )

    try:
        response = model.generate_content(prompt)
        raw_text = response.text
    except Exception as e:
        raise LLMResponseError(f"Gemini API call failed: {e}")

    cleaned = _strip_code_fences(raw_text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMResponseError(f"LLM did not return valid JSON: {e}\nRaw output: {raw_text[:500]}")

    return _validate_schema(data)


if __name__ == "__main__":
    # Smoke test — requires GEMINI_API_KEY to actually succeed.
    from ai.prompts import build_analysis_prompt
    dummy_context = {"disk": {"used_percent": 92, "mountpoint": "/"}}
    try:
        result = call_llm(build_analysis_prompt("Why is my disk almost full?", dummy_context))
        print(json.dumps(result, indent=2))
    except LLMResponseError as e:
        print(f"Expected failure without API key configured: {e}")
