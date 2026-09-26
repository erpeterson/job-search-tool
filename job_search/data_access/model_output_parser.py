"""Parse JSON returned by external model adapters."""

from __future__ import annotations

import json
import re
from typing import Any


def parse_model_json(output_text: str) -> Any:
    """Extract one JSON object from a model response or raise a JSON error."""
    if not output_text:
        raise json.JSONDecodeError("empty response", "", 0)
    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise
