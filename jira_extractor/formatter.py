from __future__ import annotations

from typing import Any


def adf_to_text(value: Any) -> str:
    """Convert Atlassian Document Format content to readable plain text."""
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        node_type = value.get("type")
        if node_type in {"hardBreak", "rule"}:
            return "\n"

        text = value.get("text")
        if isinstance(text, str):
            return text

        content = value.get("content")
        if isinstance(content, list):
            chunks = [adf_to_text(item) for item in content]
            joined = "".join(chunks)
            if node_type in {"paragraph", "heading"}:
                return f"{joined}\n"
            if node_type in {"bulletList", "orderedList"}:
                return f"{joined}\n"
            if node_type == "listItem":
                stripped = joined.strip()
                return f"- {stripped}\n" if stripped else ""
            return joined

        return ""

    if isinstance(value, list):
        return "".join(adf_to_text(item) for item in value)

    return str(value)
