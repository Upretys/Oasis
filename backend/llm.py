"""One forced tool call to Claude, cached on disk by request hash. Never logs the API key."""
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "llm"
load_dotenv(ROOT / ".env")

# These models reject a forced tool_choice; they get "auto" plus the prompt's "call X exactly once".
NO_FORCED_TOOL = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2",
                  "claude-mythos-5-1", "claude-mythos-5-2"}


def model() -> str:
    return os.environ.get("BOARD_MODEL", "claude-haiku-5-5")


def _client():
    import anthropic
    return anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the environment


def call_tool(system: str, messages: list[dict], tool: dict, client=None) -> list[dict]:
    """Return the response content blocks as plain dicts. Cache hit = no network call."""
    m = model()
    request = {"model": m, "max_tokens": 4096, "system": system, "messages": messages, "tools": [tool]}
    if m in NO_FORCED_TOOL:
        request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    else:
        request["tool_choice"] = {"type": "tool", "name": tool["name"]}
        request["thinking"] = {"type": "disabled"}
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
    path = CACHE / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    response = (client or _client()).messages.create(**request)
    content = [b for b in response.model_dump()["content"] if b["type"] in ("text", "tool_use")]
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    return content


def tool_input(content: list[dict], name: str) -> dict | None:
    return next((b["input"] for b in content if b["type"] == "tool_use" and b["name"] == name), None)
