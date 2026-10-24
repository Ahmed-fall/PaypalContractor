"""Default Leash policy (plan A.2.2 / 5.2) and the built-in, non-removable rules."""

from __future__ import annotations

from decimal import Decimal

from ..schemas.contracts import GuardAction, LeashDecision, LeashPolicy, LeashRule

_INVOICE = GuardAction.SEND_REMEDIATION_INVOICE

_G05 = LeashRule(
    id="G-05",
    applies_to=_INVOICE,
    when={"line_items_sum_mismatch": True},
    outcome=LeashDecision.DENY,
    source_text="(built in)",
)

# Enforced by the guard even when a policy omits them. Only G-05 (integrity).
BUILTIN_RULES: tuple[LeashRule, ...] = (_G05,)

_CLEAR_CONTRACT_TEXT = (
    "Send correction invoices on my own if they are $250 or less "
    "and the contract is clear."
)


def default_policy() -> LeashPolicy:
    """Return a fresh copy of the seeded default policy (version 1)."""
    return LeashPolicy(
        version=1,
        payroll_margin_limit_pct=Decimal(80),
        email_change_window_days=7,
        rules=[
            LeashRule(
                id="G-01",
                applies_to=_INVOICE,
                when={
                    "amount_lte": "250.00",
                    "confidence_gte": 0.9,
                    "clause_quote_present": True,
                },
                outcome=LeashDecision.ALLOW,
                source_text=_CLEAR_CONTRACT_TEXT,
            ),
            LeashRule(
                id="G-02",
                applies_to=_INVOICE,
                when={"amount_gt": "250.00"},
                outcome=LeashDecision.ASK,
                source_text=_CLEAR_CONTRACT_TEXT,
            ),
            LeashRule(
                id="G-03",
                applies_to=_INVOICE,
                when={"client_tag": "never_auto_contact"},
                outcome=LeashDecision.ASK,
                source_text="Always ask me first for clients tagged never_auto_contact",
            ),
            LeashRule(
                id="G-04",
                applies_to=_INVOICE,
                when={"invoiced_same_client_within_days": 7},
                outcome=LeashDecision.ASK,
                source_text="or if we already invoiced them this week.",
            ),
            _G05.model_copy(deep=True),
        ],
    )
