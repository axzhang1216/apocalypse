"""Internal compatibility shim for legacy Apocalypse analysis code.

This is intentionally named `anthropic.py`: scripts in this directory that
historically imported the Anthropic SDK now resolve this local module first.
The old call shape remains intact, but every request is routed through the
provider/model selected in Apocalypse's own analysis_harness configuration.

The legacy `model=` argument is deliberately ignored. The setup wizard's
analysis model is the single source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import analysis_harness


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _Response:
    content: list[_TextBlock]


class _Messages:
    def create(self, *, model: str | None = None, max_tokens: int = 1024,
               messages: list[dict[str, Any]] | None = None, system: Any = None,
               **_: Any) -> _Response:
        parts = []
        for m in messages or []:
            role = str(m.get("role") or "user")
            content = m.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
            else:
                text = str(content or "")
            if text:
                parts.append(text if role == "user" else f"[{role}]\n{text}")
        prompt = "\n\n".join(parts)
        system_text = system if isinstance(system, str) else None
        text = analysis_harness.complete(prompt, max_tokens=max_tokens, system=system_text)
        return _Response([_TextBlock(text)])


class Anthropic:
    def __init__(self, *args: Any, **kwargs: Any):
        # Old code may instantiate with SDK-style args. They are intentionally
        # ignored because harness.json owns runtime provider configuration.
        self.messages = _Messages()
