from __future__ import annotations

from typing import List, Any
from openai import OpenAI
import re


def make_client(api_key: str, base_url: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url)


def _extract_output_text(resp: Any) -> str:
    txt = getattr(resp, "output_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt.strip()

    out = getattr(resp, "output", None) or []
    parts: List[str] = []

    for item in out:
        if getattr(item, "type", None) == "message":
            content = getattr(item, "content", None) or []
            for c in content:
                if getattr(c, "type", None) == "output_text":
                    t = getattr(c, "text", "")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
                maybe_text = getattr(c, "value", None)
                if isinstance(maybe_text, str) and maybe_text.strip():
                    parts.append(maybe_text.strip())

    return "\n".join(parts).strip()


def _diag(resp: Any) -> str:
    status = getattr(resp, "status", None)
    incomplete_details = getattr(resp, "incomplete_details", None)
    error = getattr(resp, "error", None)

    pieces = []
    if status:
        pieces.append(f"status={status}")
    if incomplete_details:
        pieces.append(f"incomplete_details={incomplete_details}")
    if error:
        pieces.append(f"error={error}")
    return ", ".join(pieces) if pieces else "no_diagnostics"


def _sanitize_telegram_plain_text(s: str) -> str:
    if not s:
        return s

    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # Remove markdown remnants if model emits them
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", s)
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"(?m)^\s*\*\s+", "- ", s)
    s = s.replace("```", "")

    # Remove "(пусто)" lines and collapse blanks
    s = re.sub(r"(?mi)^\s*\(пусто\)\s*$", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    # Strip accidental TL;DR / Action prefixes if they appear
    s = re.sub(r"(?mi)^\s*TL;DR:\s*", "", s)
    s = re.sub(r"(?mi)^\s*Action:\s*.*$", "", s)
    s = s.strip()

    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def summarize_house_chat_messages(
    client: OpenAI,
    model: str,
    house_name: str,
    messages_blob: str,
    max_output_tokens: int,
) -> str:
    prompt = f"""
Сделай короткую сводку обсуждений в домовом чате.

КРИТИЧНО:
- Верни только ОДНУ строку на русском языке, 8-30 слов.
- Без markdown.
- Только факты из сообщений, без выдумок.
- Если сообщений нет или они пустые, верни: новых обсуждений нет.

Дом: {house_name}

Сообщения:
{messages_blob}
""".strip()

    resp = client.responses.create(
        model=model,
        input=prompt,
        max_output_tokens=max_output_tokens,
    )

    text = _extract_output_text(resp)
    if not text:
        raise RuntimeError(f"Empty house chat summary ({_diag(resp)})")

    text = _sanitize_telegram_plain_text(text)
    return " ".join(text.split()).strip()
