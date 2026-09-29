"""Parse JSON returned by external model adapters."""

from __future__ import annotations

import json
import re
from typing import Any

from job_search.application.contracts import Telemetry


def parse_model_json(output_text: str, telemetry: Telemetry) -> Any:
    """Extract one JSON object from a model response or raise a JSON error."""
    if not output_text:
        raise json.JSONDecodeError("empty response", "", 0)
    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            # A prose wrapper is recoverable only when its embedded object parses cleanly.
            parsed = json.loads(cleaned[start : end + 1])
            telemetry.event(
                "model_output_fence_recovered",
                error_code="MODEL_OUTPUT_FENCE_RECOVERED",
                component="data_access.model_output_parser",
                operation="extract_json_object",
                output_length=len(cleaned),
                cause=type(exc).__name__,
            )
            return parsed
        raise
