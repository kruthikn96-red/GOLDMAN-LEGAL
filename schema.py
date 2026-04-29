"""Pydantic schema for executable policy rules.

v2 represents rules as scoped executable objects:
  - per-entity predicates compile to WHERE clauses,
  - portfolio aggregates compile to HAVING clauses,
  - portfolio subset shares compile to group/share constraints,
  - fees compile to event-gated amount functions.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


ComparisonOp = Literal[
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "between",
    "is_null",
    "is_not_null",
]
LogicalOp = Literal["and", "or", "not"]
ArithOp = Literal["multiply", "add", "subtract", "divide"]
Unit = Literal["percent", "score", "USD", "days", "months", "years"]


class Predicate(BaseModel):
    op: ComparisonOp
    attr: str
    value: Optional[Union[float, int, str, bool, list[Union[float, int, str, bool]]]] = None
    unmapped: bool = False
    original_phrase: Optional[str] = None


class LogicalExpr(BaseModel):
    op: LogicalOp
    args: list["Expression"]

    @model_validator(mode="after")
    def validate_args(self) -> "LogicalExpr":
        if self.op == "not" and len(self.args) != 1:
            raise ValueError("not expressions must have exactly one arg")
        if self.op in {"and", "or"} and len(self.args) < 2:
            raise ValueError("and/or expressions must have at least two args")
        return self


Expression = Annotated[Union[Predicate, LogicalExpr], Field(discriminator="op")]


class AggregateSpec(BaseModel):
    function: Literal["weighted_avg", "avg", "sum", "count", "min", "max"]
    attr: str
    weight_attr: Optional[str] = None


class AggregateConstraint(BaseModel):
    op: ComparisonOp
    value: float
    unit: Optional[Unit] = None


class ShareConstraint(BaseModel):
    op: Literal["lte", "gte"]
    value: float
    unit: Literal["percent"] = "percent"


class FormulaRef(BaseModel):
    rule_ref: str
    field: str


class FormulaValue(BaseModel):
    value: float


class ArithmeticFormula(BaseModel):
    op: ArithOp
    args: list["Formula"]


Formula = Union[FormulaRef, FormulaValue, ArithmeticFormula]


class FeeAmount(BaseModel):
    method: Literal["fixed", "percentage", "derived"]
    value_usd: Optional[float] = None
    rate: Optional[float] = None
    unit: Optional[Literal["percent", "percent_per_annum"]] = None
    of_attr: Optional[str] = None
    min_usd: Optional[float] = None
    max_usd: Optional[float] = None
    formula: Optional[Formula] = None

    @model_validator(mode="after")
    def validate_amount_for_method(self) -> "FeeAmount":
        if self.method == "fixed" and self.value_usd is None:
            raise ValueError(
                "fee.amount.method='fixed' requires value_usd (the dollar amount)"
            )
        if self.method == "percentage" and (self.rate is None or self.of_attr is None):
            raise ValueError(
                "fee.amount.method='percentage' requires both rate and of_attr"
            )
        if self.method == "derived" and self.formula is None:
            raise ValueError(
                "fee.amount.method='derived' requires a formula"
            )
        return self


class FeeTrigger(BaseModel):
    event: Literal[
        "application_submission",
        "payment_overdue",
        "policy_cancellation",
        "policy_reinstatement",
        "policy_renewal",
        "ongoing",
    ]
    when: Optional[Predicate] = None


class PerEntityRule(BaseModel):
    scope: Literal["per_entity"] = "per_entity"
    entity: Literal["applicant", "policy", "co_signer"]
    must_satisfy: Expression
    evaluation_context: Optional[dict[str, Any]] = None


class PortfolioAggregateRule(BaseModel):
    scope: Literal["portfolio_aggregate"] = "portfolio_aggregate"
    aggregate: AggregateSpec
    must_satisfy: AggregateConstraint


class PortfolioSubsetShareRule(BaseModel):
    scope: Literal["portfolio_subset_share"] = "portfolio_subset_share"
    subset: Optional[Expression] = None
    subset_groupby: Optional[str] = None
    share_of: str
    share_constraint: ShareConstraint

    @model_validator(mode="after")
    def validate_subset_shape(self) -> "PortfolioSubsetShareRule":
        if bool(self.subset) == bool(self.subset_groupby):
            raise ValueError("exactly one of subset or subset_groupby is required")
        return self


class FeeRule(BaseModel):
    scope: Literal["fee"] = "fee"
    name: str
    trigger: FeeTrigger
    amount: FeeAmount
    frequency: Optional[Literal["per_application", "monthly", "annual", "per_event"]] = None
    payment_terms: Optional[str] = None


RuleBody = Annotated[
    Union[PerEntityRule, PortfolioAggregateRule, PortfolioSubsetShareRule, FeeRule],
    Field(discriminator="scope"),
]


class Rule(BaseModel):
    rule_id: str
    section: str
    subsection: str
    source_text: str
    confidence: Literal["high", "medium", "low"]
    unmapped_attributes: list[str] = Field(default_factory=list)
    body: RuleBody


class Document(BaseModel):
    document_id: str
    section: str
    section_title: str
    rules: list[Rule]


def extraction_payload_schema() -> dict:
    """JSON Schema for the LLM response for a single subsection."""
    return {
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": Rule.model_json_schema(),
                "description": (
                    "One or more executable rules extracted from the subsection. "
                    "A single subsection can encode multiple distinct rules."
                ),
            }
        },
        "required": ["rules"],
    }
