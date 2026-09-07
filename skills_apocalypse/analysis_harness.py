#!/usr/bin/env python3
"""Provider-independent LLM harness owned by Apocalypse.

The harness consumes only Apocalypse's own generated configuration. Existing
agent configs are discovery inputs, never runtime dependencies except when the
selected transport intentionally uses that agent's authenticated CLI.

Supported transports:
- anthropic_messages   HTTP POST /v1/messages
- openai_responses     HTTP POST /v1/responses
- openai_chat          HTTP POST /v1/chat/completions
- claude_cli           authenticated Claude Code print mode
- codex_cli            authenticated Codex non-interactive exec mode

No provider SDK dependency is required.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path.home() / ".claude" / "apocalypse"
CONFIG_FILE = DATA_DIR / "harness.json"


class HarnessError(RuntimeError):
    pass


def load_config() -> dict[str, Any]:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HarnessError("Apocalypse analysis model is not configured. Run: apocalypse-ui init") from exc
    except Exception as exc:
        raise HarnessError(f"Could not read {CONFIG_FILE}: {exc}") from exc
    if not isinstance(cfg, dict) or not isinstance(cfg.get("analysis_model"), dict):
        raise HarnessError("Invalid harness.json: missing analysis_model")
    return cfg


def model_config() -> dict[str, Any]:
    return dict(load_config()["analysis_model"])


def _secret(auth: Any) -> str | None:
    if not isinstance(auth, dict):
        return None
    kind = auth.get("kind")
    if kind == "env":
        name = str(auth.get("name") or "")
        return os.environ.get(name) if name else None
    if kind == "file":
        try:
            p = Path(os.path.expandvars(os.path.expanduser(str(auth.get("path") or ""))))
            data = json.loads(p.read_text(encoding="utf-8"))
            cur: Any = data
            for part in str(auth.get("json_path") or "").split("."):
                if part:
                    cur = cur[part]
            return str(cur) if cur is not None else None
        except Exception:
            return None
    if kind == "literal":
        # Kept for migration/back-compat. setup_wizard does not persist copied
        # literal secrets; it moves them to secrets.json and references that file.
        return str(auth.get("value") or "") or None
    return None


def _headers(cfg: dict[str, Any], anthropic: bool = False) -> dict[str, str]:
    h = {"content-type": "application/json", "accept": "application/json", "user-agent": "Apocalypse-Harness/1"}
    token = _secret(cfg.get("auth"))
    if token:
        if anthropic:
            h["x-api-key"] = token
            h["anthropic-version"] = "2023-06-01"
        else:
            h["authorization"] = "Bearer " + token
    for k, v in (cfg.get("headers") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            h[k] = os.path.expandvars(v)
    return h


def _url(base: str, suffix: str) -> str:
    b = str(base or "").rstrip("/")
    if not b:
        raise HarnessError("Selected HTTP analysis provider has no base_url")
    path = urllib.request.urlparse(b).path if hasattr(urllib.request, "urlparse") else ""
    if b.endswith(suffix):
        return b
    if b.endswith("/v1") and suffix.startswith("/v1/"):
        return b + suffix[len("/v1"):]
    return b + suffix


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            detail = ""
        raise HarnessError(f"Provider HTTP {e.code}: {detail or e.reason}") from e
    except Exception as e:
        raise HarnessError(f"Provider request failed: {e}") from e
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        raise HarnessError("Provider returned non-JSON output") from e


def _anthropic(cfg: dict[str, Any], prompt: str, max_tokens: int, system: str | None, timeout: float) -> str:
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    data = _post_json(_url(cfg.get("base_url") or "https://api.anthropic.com", "/v1/messages"), payload, _headers(cfg, True), timeout)
    content = data.get("content") if isinstance(data, dict) else None
    if isinstance(content, list):
        text = "".join(str(x.get("text") or "") for x in content if isinstance(x, dict) and x.get("type") in (None, "text"))
        if text.strip():
            return text.strip()
    raise HarnessError("Anthropic-compatible response did not contain text")


def _responses_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    out = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text"):
                out.append(str(c["text"]))
    return "\n".join(out).strip()


def _openai_responses(cfg: dict[str, Any], prompt: str, max_tokens: int, system: str | None, timeout: float) -> str:
    text = prompt if not system else system + "\n\n" + prompt
    payload = {"model": cfg["model"], "input": text, "max_output_tokens": max_tokens}
    data = _post_json(_url(cfg.get("base_url") or "https://api.openai.com", "/v1/responses"), payload, _headers(cfg), timeout)
    out = _responses_text(data)
    if out:
        return out
    raise HarnessError("Responses-compatible response did not contain output_text")


def _openai_chat(cfg: dict[str, Any], prompt: str, max_tokens: int, system: str | None, timeout: float) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": cfg["model"], "messages": messages, "max_tokens": max_tokens}
    data = _post_json(_url(cfg.get("base_url") or "https://api.openai.com", "/v1/chat/completions"), payload, _headers(cfg), timeout)
    try:
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, str) and content.strip():
            return content.strip()
    except Exception:
        pass
    raise HarnessError("Chat-compatible response did not contain assistant text")


def _run_cli(args: list[str], prompt: str, timeout: float, env: dict[str, str] | None = None) -> str:
    kwargs: dict[str, Any] = {
        "input": prompt,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env or os.environ.copy(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as e:
        raise HarnessError(f"Analysis CLI timed out after {timeout:.0f}s") from e
    except Exception as e:
        raise HarnessError(f"Could not run analysis CLI: {e}") from e
    if p.returncode != 0:
        raise HarnessError((p.stderr or p.stdout or f"CLI exited {p.returncode}").strip()[:1200])
    if not (p.stdout or "").strip():
        raise HarnessError("Analysis CLI returned empty output")
    return p.stdout.strip()


def _claude_cli(cfg: dict[str, Any], prompt: str, max_tokens: int, system: str | None, timeout: float) -> str:
    exe = cfg.get("executable") or "claude"
    model = cfg.get("model") or "sonnet"
    full = prompt if not system else system + "\n\n" + prompt
    # Print mode is stateless and doesn't open/modify project sessions.
    args = [str(exe), "-p", "--model", str(model), "--output-format", "text"]
    return _run_cli(args, full, timeout)


def _codex_cli(cfg: dict[str, Any], prompt: str, max_tokens: int, system: str | None, timeout: float) -> str:
    exe = cfg.get("executable") or "codex"
    model = cfg.get("model")
    full = prompt if not system else system + "\n\n" + prompt
    args = [str(exe), "exec", "--skip-git-repo-check"]
    if model and model != "(Codex default)":
        args += ["--model", str(model)]
    args += ["-"]
    return _run_cli(args, full, timeout)


def complete(prompt: str, *, max_tokens: int = 1024, system: str | None = None,
             timeout: float = 120.0, model_override: str | None = None) -> str:
    cfg = model_config()
    if model_override:
        cfg["model"] = model_override
    transport = str(cfg.get("transport") or "")
    if transport == "anthropic_messages":
        return _anthropic(cfg, prompt, max_tokens, system, timeout)
    if transport == "openai_responses":
        return _openai_responses(cfg, prompt, max_tokens, system, timeout)
    if transport == "openai_chat":
        return _openai_chat(cfg, prompt, max_tokens, system, timeout)
    if transport == "claude_cli":
        return _claude_cli(cfg, prompt, max_tokens, system, timeout)
    if transport == "codex_cli":
        return _codex_cli(cfg, prompt, max_tokens, system, timeout)
    raise HarnessError(f"Unsupported Apocalypse analysis transport: {transport}")


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            t = "\n".join(lines[1:-1]).strip()
    return t


def complete_json(prompt: str, *, max_tokens: int = 1024, system: str | None = None,
                  timeout: float = 120.0) -> Any:
    text = _strip_fence(complete(prompt, max_tokens=max_tokens, system=system, timeout=timeout))
    try:
        return json.loads(text)
    except Exception:
        # Models occasionally wrap JSON with one sentence despite instructions.
        m = re.search(r"(?:\{|\[).*(?:\}|\])", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        raise HarnessError("Analysis model returned invalid JSON")


def test() -> dict[str, Any]:
    cfg = model_config()
    text = complete("Reply with exactly APOCALYPSE_OK and nothing else.", max_tokens=32, timeout=60)
    return {
        "ok": "APOCALYPSE_OK" in text,
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "transport": cfg.get("transport"),
        "response": text[:120],
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true")
    p.add_argument("prompt", nargs="?")
    a = p.parse_args()
    try:
        result = test() if a.test else complete(a.prompt or "Reply with APOCALYPSE_OK")
        print(json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else result)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        raise SystemExit(1)
