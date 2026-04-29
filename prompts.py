"""LLM prompts for executable v2 rule extraction."""

from __future__ import annotations

import json


SYSTEM_PROMPT_TEMPLATE = """\
You are a legal-policy analyst extracting executable policy rules.

You will be given ONE subsection of a policy, already split out with rule_id,
section, subsection, and source_text. Return JSON with a `rules` array
containing one or more Rule objects. A subsection can contain multiple rules.

# Attribute vocabulary

You may ONLY use attribute names from this closed vocabulary:

{vocabulary}

If the policy mentions a concept not in the vocabulary, use the closest
available executable rule when possible. If no safe mapping exists, set the
predicate field `unmapped` to true, put the source phrase in `original_phrase`,
include the phrase in top-level `unmapped_attributes`, and set confidence low.

# Predicate expression language

Every executable condition is an expression tree:
- Leaf: {{"op": "gte", "attr": "applicant.credit_score", "value": 680}}
- Compound: {{"op": "and", "args": [expr, expr]}}
- Compound: {{"op": "or", "args": [expr, expr]}}
- Negation: {{"op": "not", "args": [expr]}}

Allowed comparison ops:
eq, neq, gt, gte, lt, lte, in, not_in, between, is_null, is_not_null.
Use `between` with value [min, max], inclusive.

Operator mapping:
- "at least", "no less than", "minimum" -> gte
- "above", "more than", "exceeding" -> gt
- "shall not exceed", "no more than", "maximum", "up to" -> lte
- "below", "under", "less than" -> lt

Negative wrappers reverse direction. Always express the rule as what MUST
be true, never as what is forbidden:
- "No applicant shall have income below $X" -> income gte X (NOT lt X).
- "must not have any payment more than N days overdue" -> the applicant's
  MAX overdue days must be lte N, i.e.
  applicant.payment_overdue_days_max lte N
  (per-applicant aggregate, not per-payment).

# Attribute selection (avoid common mistakes)

- "any payment more than N days overdue" on the APPLICANT -> use
  `applicant.payment_overdue_days_max`. The catalog's `payment.overdue_days`
  is per-payment-event and is used ONLY inside fee `trigger.when` predicates,
  never for per-applicant eligibility checks.
- "United States or its territories" -> value list ["US", "US_TERRITORY"].
  Do NOT enumerate individual territories like "PR", "GU", or "VI" — the
  vocabulary's enum is closed and the database does not store ISO codes.
- "primary applicant under N years of age" inside a portfolio rule ->
  AND `policy.applicant.is_primary == true` with the age predicate inside
  `subset`.
- Recurring fees with no triggering event use trigger.event = "ongoing"
  and a `frequency` (monthly / annual). Do NOT invent ad-hoc events.
- "N months of [another fee]" in a fee amount -> method "derived" with a
  formula referencing the other rule by `rule_ref` (and the other rule's
  field name, usually "monthly_amount").

# Exceptions and unless clauses

Represent exceptions as OR trees:
- base branch: the normal requirement
- exception branch: an AND of the exception trigger and the alternate threshold

Example:
"DTI shall not exceed 40%, or 45% if co-signer credit score above 750"
becomes:
or([
  dti <= 40,
  and([cosigner.credit_score > 750, dti <= 45])
])

# Scope selection

- `per_entity`: "Each applicant", "The applicant", "any single policy".
- `portfolio_aggregate`: weighted average or average across portfolio.
- `portfolio_subset_share`: no more than X% of total portfolio value.
- Use `subset_groupby` for "any single state" / "any one state".
- `fee`: section title contains FEES/CHARGES or subsection defines a fee.

# Evaluation context

When the source text references a specific evaluation moment ("as of the
Review Date", "on the policy effective date", "at the time of underwriting"),
preserve it on the rule body as:

  "evaluation_context": {{"as_of": "review_date"}}

Snake_case the named date. NEVER drop temporal qualifiers; they are
material for audit and compliance reproducibility.

# Output rules

- Always include rule_id, section, subsection, source_text exactly.
- For multiple rules from one subsection, suffix ids as `.1`, `.2`.
- Use confidence high for unambiguous mappings, medium for reasonable choices,
  and low for unmapped/ambiguous rules.
- Match enum values exactly. If the vocabulary declares a closed set of
  values for an attribute, pick from those values verbatim — do not invent
  alternative codes.
- Preserve evaluation_context whenever the source text contains "as of",
  "on the date of", or any other temporal scope.
- `rule_ref` must point to a rule_id in the SAME document. Use the section
  prefix of the rule being extracted, such as `12.2.x` for rules in section
  12.2. Never reuse rule_ids from few-shot examples.
- Return only JSON. No prose.
"""


def render_system_prompt(allowed_attrs: list[str]) -> str:
    lines = []
    for attr in allowed_attrs:
        lines.append(f"- {attr}")
    return SYSTEM_PROMPT_TEMPLATE.format(vocabulary="\n".join(lines))


def render_user_message(chunk: dict) -> str:
    return (
        "Extract executable v2 rule(s) from this subsection. Return JSON with "
        "a `rules` array.\n\n"
        + json.dumps(chunk, indent=2)
    )


FEW_SHOT_EXAMPLES = [
    {
        "input": {
            "rule_id": "1.1.a",
            "section": "1.1",
            "section_title": "EXAMPLE",
            "subsection": "a",
            "source_text": "Each applicant must have a credit score of at least 650.",
        },
        "output": {
            "rules": [
                {
                    "rule_id": "1.1.a",
                    "section": "1.1",
                    "subsection": "a",
                    "source_text": "Each applicant must have a credit score of at least 650.",
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "per_entity",
                        "entity": "applicant",
                        "must_satisfy": {
                            "op": "gte",
                            "attr": "applicant.credit_score",
                            "value": 650,
                        },
                    },
                }
            ]
        },
    },
    {
        "input": {
            "rule_id": "1.1.b",
            "section": "1.1",
            "section_title": "EXAMPLE",
            "subsection": "b",
            "source_text": (
                "The annual income must be at least $40,000, or $30,000 if "
                "the applicant is enrolled in an approved assistance program."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "1.1.b",
                    "section": "1.1",
                    "subsection": "b",
                    "source_text": (
                        "The annual income must be at least $40,000, or $30,000 if "
                        "the applicant is enrolled in an approved assistance program."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "per_entity",
                        "entity": "applicant",
                        "must_satisfy": {
                            "op": "or",
                            "args": [
                                {
                                    "op": "gte",
                                    "attr": "applicant.annual_income_usd",
                                    "value": 40000,
                                },
                                {
                                    "op": "and",
                                    "args": [
                                        {
                                            "op": "eq",
                                            "attr": (
                                                "applicant.enrolled_in_approved_"
                                                "assistance_program"
                                            ),
                                            "value": True,
                                        },
                                        {
                                            "op": "gte",
                                            "attr": "applicant.annual_income_usd",
                                            "value": 30000,
                                        },
                                    ],
                                },
                            ],
                        },
                    },
                }
            ]
        },
    },
    {
        "input": {
            "rule_id": "2.1.a",
            "section": "2.1",
            "section_title": "EXAMPLE",
            "subsection": "a",
            "source_text": (
                "The weighted average credit score across the portfolio shall "
                "not be less than 710."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "2.1.a",
                    "section": "2.1",
                    "subsection": "a",
                    "source_text": (
                        "The weighted average credit score across the portfolio shall "
                        "not be less than 710."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "portfolio_aggregate",
                        "aggregate": {
                            "function": "weighted_avg",
                            "attr": "applicant.credit_score",
                            "weight_attr": "policy.coverage_amount_usd",
                        },
                        "must_satisfy": {"op": "gte", "value": 710},
                    },
                }
            ]
        },
    },
    {
        "input": {
            "rule_id": "2.1.b",
            "section": "2.1",
            "section_title": "EXAMPLE",
            "subsection": "b",
            "source_text": (
                "No more than 12% of total portfolio value may consist of "
                "policies with coverage above $1,000,000."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "2.1.b",
                    "section": "2.1",
                    "subsection": "b",
                    "source_text": (
                        "No more than 12% of total portfolio value may consist of "
                        "policies with coverage above $1,000,000."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "portfolio_subset_share",
                        "subset": {
                            "op": "gt",
                            "attr": "policy.coverage_amount_usd",
                            "value": 1000000,
                        },
                        "share_of": "total_portfolio_value_usd",
                        "share_constraint": {"op": "lte", "value": 12, "unit": "percent"},
                    },
                }
            ]
        },
    },
    {
        "input": {
            "rule_id": "2.1.c",
            "section": "2.1",
            "section_title": "EXAMPLE",
            "subsection": "c",
            "source_text": (
                "No more than 30% of total portfolio value may originate from "
                "any single state."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "2.1.c",
                    "section": "2.1",
                    "subsection": "c",
                    "source_text": (
                        "No more than 30% of total portfolio value may originate from "
                        "any single state."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "portfolio_subset_share",
                        "subset_groupby": "policy.origin_state",
                        "share_of": "total_portfolio_value_usd",
                        "share_constraint": {"op": "lte", "value": 30, "unit": "percent"},
                    },
                }
            ]
        },
    },
    {
        "input": {
            "rule_id": "3.1.a",
            "section": "3.1",
            "section_title": "FEES",
            "subsection": "a",
            "source_text": (
                "Late Fee: If payment is more than 10 days overdue, a fee of "
                "1% applies, subject to a minimum of $10 and maximum of $100."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "3.1.a",
                    "section": "3.1",
                    "subsection": "a",
                    "source_text": (
                        "Late Fee: If payment is more than 10 days overdue, a fee of "
                        "1% applies, subject to a minimum of $10 and maximum of $100."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "fee",
                        "name": "Late Fee",
                        "trigger": {
                            "event": "payment_overdue",
                            "when": {
                                "op": "gt",
                                "attr": "payment.overdue_days",
                                "value": 10,
                            },
                        },
                        "amount": {
                            "method": "percentage",
                            "rate": 1,
                            "unit": "percent",
                            "of_attr": "payment.overdue_amount_usd",
                            "min_usd": 10,
                            "max_usd": 100,
                        },
                    },
                }
            ]
        },
    },
    {
        "input": {
            "rule_id": "4.1.a",
            "section": "4.1",
            "section_title": "EXAMPLE",
            "subsection": "a",
            "source_text": (
                "The applicant must have a credit score of at least 650, provided "
                "that no more than 20% of total portfolio value may consist of "
                "applicants with credit scores between 650 and 690."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "4.1.a.1",
                    "section": "4.1",
                    "subsection": "a",
                    "source_text": (
                        "The applicant must have a credit score of at least 650, provided "
                        "that no more than 20% of total portfolio value may consist of "
                        "applicants with credit scores between 650 and 690."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "per_entity",
                        "entity": "applicant",
                        "must_satisfy": {
                            "op": "gte",
                            "attr": "applicant.credit_score",
                            "value": 650,
                        },
                    },
                },
                {
                    "rule_id": "4.1.a.2",
                    "section": "4.1",
                    "subsection": "a",
                    "source_text": (
                        "The applicant must have a credit score of at least 650, provided "
                        "that no more than 20% of total portfolio value may consist of "
                        "applicants with credit scores between 650 and 690."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "portfolio_subset_share",
                        "subset": {
                            "op": "between",
                            "attr": "applicant.credit_score",
                            "value": [650, 690],
                        },
                        "share_of": "total_portfolio_value_usd",
                        "share_constraint": {"op": "lte", "value": 20, "unit": "percent"},
                    },
                },
            ]
        },
    },
    # ---- Per-applicant aggregate + evaluation_context ----
    {
        "input": {
            "rule_id": "1.1.c",
            "section": "1.1",
            "section_title": "EXAMPLE",
            "subsection": "c",
            "source_text": (
                "The applicant must not have any payment more than 45 days "
                "overdue as of the policy effective date."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "1.1.c",
                    "section": "1.1",
                    "subsection": "c",
                    "source_text": (
                        "The applicant must not have any payment more than 45 days "
                        "overdue as of the policy effective date."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "per_entity",
                        "entity": "applicant",
                        "must_satisfy": {
                            "op": "lte",
                            "attr": "applicant.payment_overdue_days_max",
                            "value": 45,
                        },
                        "evaluation_context": {"as_of": "policy_effective_date"},
                    },
                }
            ]
        },
    },
    # ---- Compound subset filter (primary applicant + age) ----
    {
        "input": {
            "rule_id": "2.1.d",
            "section": "2.1",
            "section_title": "EXAMPLE",
            "subsection": "d",
            "source_text": (
                "No more than 8% of total portfolio value may consist of "
                "policies where the primary applicant is over 70 years of age."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "2.1.d",
                    "section": "2.1",
                    "subsection": "d",
                    "source_text": (
                        "No more than 8% of total portfolio value may consist of "
                        "policies where the primary applicant is over 70 years of age."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "portfolio_subset_share",
                        "subset": {
                            "op": "and",
                            "args": [
                                {
                                    "op": "eq",
                                    "attr": "policy.applicant.is_primary",
                                    "value": True,
                                },
                                {
                                    "op": "gt",
                                    "attr": "applicant.age_years",
                                    "value": 70,
                                },
                            ],
                        },
                        "share_of": "total_portfolio_value_usd",
                        "share_constraint": {"op": "lte", "value": 8, "unit": "percent"},
                    },
                }
            ]
        },
    },
    # ---- Recurring percentage fee (ongoing trigger, monthly frequency) ----
    {
        "input": {
            "rule_id": "3.1.b",
            "section": "3.1",
            "section_title": "FEES",
            "subsection": "b",
            "source_text": (
                "Service Fee: 0.5% per annum of the outstanding coverage "
                "amount, payable monthly."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "3.1.b",
                    "section": "3.1",
                    "subsection": "b",
                    "source_text": (
                        "Service Fee: 0.5% per annum of the outstanding coverage "
                        "amount, payable monthly."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "fee",
                        "name": "Service Fee",
                        "trigger": {"event": "ongoing"},
                        "amount": {
                            "method": "percentage",
                            "rate": 0.5,
                            "unit": "percent_per_annum",
                            "of_attr": "policy.outstanding_coverage_amount_usd",
                        },
                        "frequency": "monthly",
                    },
                }
            ]
        },
    },
    # ---- Derived fee (formula referencing another rule) ----
    {
        "input": {
            "rule_id": "3.1.c",
            "section": "3.1",
            "section_title": "FEES",
            "subsection": "c",
            "source_text": (
                "Cancellation Fee: If the policy is cancelled within the first "
                "12 months, the applicant shall pay a fee equal to 2 months "
                "of the service fee."
            ),
        },
        "output": {
            "rules": [
                {
                    "rule_id": "3.1.c",
                    "section": "3.1",
                    "subsection": "c",
                    "source_text": (
                        "Cancellation Fee: If the policy is cancelled within the first "
                        "12 months, the applicant shall pay a fee equal to 2 months "
                        "of the service fee."
                    ),
                    "confidence": "high",
                    "unmapped_attributes": [],
                    "body": {
                        "scope": "fee",
                        "name": "Cancellation Fee",
                        "trigger": {
                            "event": "policy_cancellation",
                            "when": {
                                "op": "lte",
                                "attr": "policy.cancellation_within_months",
                                "value": 12,
                            },
                        },
                        "amount": {
                            "method": "derived",
                            "formula": {
                                "op": "multiply",
                                "args": [
                                    {"rule_ref": "3.1.b", "field": "monthly_amount"},
                                    {"value": 2},
                                ],
                            },
                        },
                    },
                }
            ]
        },
    },
]


def render_few_shot_messages() -> list[dict]:
    msgs = []
    for ex in FEW_SHOT_EXAMPLES:
        msgs.append({"role": "user", "content": render_user_message(ex["input"])})
        msgs.append({"role": "assistant", "content": json.dumps(ex["output"])})
    return msgs
