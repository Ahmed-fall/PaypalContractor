"""AI & compliance engine (owned by Person 2).

Stateless library imported by the backend. Two public functions:

- ``extract_contract_rules(file_bytes, filename)`` -> ``ContractExtractionResult``
  Raises ``ContractParseError`` (map to HTTP 422) for unusable uploads.
- ``evaluate_payment_compliance(contract, milestone, captured_amount, captured_currency,
  captured_date, payer_note="")`` -> ``DiscrepancyEvaluationResult``
  Never raises on model failure; money always comes from ``compliance_math``.

Only ``GEMINI_API_KEY`` is read (optionally ``GEMINI_MODEL`` / ``GEMINI_FALLBACK_MODEL``);
without it both functions still work through their deterministic fallbacks.
"""

from .auditor_agent import evaluate_payment_compliance
from .extractor import extract_contract_rules
from .pdf_parser import ContractParseError

__all__ = [
    "ContractParseError",
    "evaluate_payment_compliance",
    "extract_contract_rules",
]
