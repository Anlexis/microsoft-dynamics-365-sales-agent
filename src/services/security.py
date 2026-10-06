"""Input sanitizing and caller-contract validation helpers for CMN-C2-231.

Pure, stateless domain helpers (NOT framework gate methods) shared by the outer
backbone pre_process node and the inner workflow nodes. Everything here is
fail-closed by construction: a value that does not provably satisfy its stated
bound is rejected, never coerced into "probably fine".

Three families live here:

* text hygiene — HTML/control-character stripping and length caps applied to
  every caller-supplied string before it is serialized or rendered;
* shape locks — the record-identifier, inert-identifier and attribute-key
  grammars the caller-data contract accepts;
* numeric bounds — ``finite_in_range``, which rejects bools, non-numerics and
  NaN/Infinity. NaN is the dangerous case: it parses through ``float()`` and
  arrives through plain JSON, yet every comparison against it is False, so an
  unchecked NaN silently passes range checks instead of failing them.

The instruction-override screen is the template's OWN refusal: the platform
input gate refuses such payloads too, but a template whose only defence is that
gate returns success wherever the gate is absent or configured off. Refusal is
stated in terms of behaviour — error status, nothing carried forward — never in
terms of any gate's wording.
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

_HTML_TAG_RE = re.compile(r"<[^>]+>")
# C0/C1 control characters except tab / newline / carriage return. Caller text
# that renders into a confirmation message must never carry them.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

DEFAULT_MAX_LENGTH = 4000

# A Dataverse record GUID (8-4-4-4-12 hex). The only shape the caller-supplied
# record identifier is allowed to take — anything else is refused rather than
# passed through to a CRM write.
RECORD_ID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
# Caller-supplied labels that travel into audit payloads are locked to an inert
# identifier shape — free text in a caller-controlled label is a log-injection
# vector.
INERT_IDENTIFIER_RE = re.compile(r"\A[a-z0-9_]{1,32}\Z")
# Caller-supplied Dataverse attribute keys: lower-case words, spaces, hyphens
# and underscores only. Never rendered as-is into a request URL — the node maps
# them onto known logical names and folds anything unknown into a description.
ATTRIBUTE_KEY_RE = re.compile(r"\A[a-z][a-z0-9 _-]{0,39}\Z")

# Instruction-override phrasings: text addressed to the MODEL rather than a
# sales request addressed to the CRM. Deliberately narrow — an ordinary request
# that happens to contain these words ("update the lead's system prompt field")
# does not match, because every alternative requires the imperative override
# shape.
_INSTRUCTION_OVERRIDE_RE = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)"
    r"|disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules)"
    r"|forget\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts)"
    r"|(?:reveal|show|print|repeat|output|disclose)\s+(?:me\s+)?(?:your|the)\s+"
    r"(?:system\s+prompt|system\s+message|instructions|initial\s+prompt)"
    r"|you\s+are\s+now\s+(?:a|an)\s"
    r"|act\s+as\s+(?:if\s+you\s+are\s+)?(?:a\s+|an\s+)?(?:developer|admin|root)\s+mode"
    r"|override\s+(?:your|the)\s+(?:instruction|instructions|rules|safety)",
    re.IGNORECASE,
)


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML markup and control characters, then cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned[:max_length]


def is_instruction_override(text: str) -> bool:
    """True when the text is an instruction-override payload rather than a request."""
    return bool(text) and bool(_INSTRUCTION_OVERRIDE_RE.search(text))


def finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse a caller-supplied number, or return None when it is not usable.

    Rejects (returns None for): bools (``isinstance(True, int)`` is True in
    Python), non-numeric types, strings that do not parse, NaN and +/-Infinity,
    and values outside the inclusive ``[lo, hi]`` range. Callers treat None as a
    hard rejection — the value is never defaulted silently.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip().replace(",", ""))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not math.isfinite(parsed):
        return None
    if not lo <= parsed <= hi:
        return None
    return parsed


# Digits with optional grouping / decimal / exponent / sign, plus the IEEE
# special spellings - i.e. "the caller meant this to be a number". Used to tell
# a malformed NUMBER (which must fail closed) from ordinary prose.
_NUMERIC_TEXT_RE = re.compile(
    r"\A[^0-9A-Za-z]*[+-]?(?:\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?|nan|inf(?:inity)?)[^0-9A-Za-z]*\Z",
    re.IGNORECASE,
)


def looks_numeric(value: str) -> bool:
    """True when the text was clearly meant as a number (including NaN / Infinity).

    A string spelling of a non-finite value is exactly the fail-open case: it is
    not an int or a float, so a type-based check waves it through, and it turns
    into one the moment anything calls float() on it.
    """
    return bool(_NUMERIC_TEXT_RE.match(value.strip()))


def is_record_id(value: Any) -> bool:
    """True when the value is a syntactically valid Dataverse record identifier."""
    return isinstance(value, str) and bool(RECORD_ID_RE.match(value.strip()))


def is_inert_identifier(value: Any) -> bool:
    """True when the value is an inert `[a-z0-9_]{1,32}` label."""
    return isinstance(value, str) and bool(INERT_IDENTIFIER_RE.match(value))
