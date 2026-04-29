"""Compile executable v2 rules into illustrative SQL fragments.

This is a demo compiler, not a database integration. It proves the JSON shape
has enough structure to translate non-fee rules into SQL patterns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from schema import Document

OUTPUT_DIR = Path("output")
COMPILED_PATH = OUTPUT_DIR / "compiled.sql"


def col(attr: str) -> str:
    return attr.replace(".", "__")


def lit(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def expr_sql(expr: dict) -> str:
    op = expr["op"]
    if op in {"and", "or"}:
        joiner = f" {op.upper()} "
        return "(" + joiner.join(expr_sql(arg) for arg in expr["args"]) + ")"
    if op == "not":
        return f"(NOT {expr_sql(expr['args'][0])})"
    attr = col(expr["attr"])
    if op == "eq":
        return f"{attr} = {lit(expr['value'])}"
    if op == "neq":
        return f"{attr} <> {lit(expr['value'])}"
    if op in {"gt", "gte", "lt", "lte"}:
        sql_op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        return f"{attr} {sql_op} {lit(expr['value'])}"
    if op == "in":
        return f"{attr} IN ({', '.join(lit(v) for v in expr['value'])})"
    if op == "not_in":
        return f"{attr} NOT IN ({', '.join(lit(v) for v in expr['value'])})"
    if op == "between":
        low, high = expr["value"]
        return f"{attr} BETWEEN {lit(low)} AND {lit(high)}"
    if op == "is_null":
        return f"{attr} IS NULL"
    if op == "is_not_null":
        return f"{attr} IS NOT NULL"
    raise ValueError(f"Unsupported op: {op}")


def constraint_sql(constraint: dict, lhs: str) -> str:
    op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "="}[constraint["op"]]
    return f"{lhs} {op} {lit(constraint['value'])}"


def compile_rule(rule: dict) -> str:
    body = rule["body"]
    rid = rule["rule_id"]
    scope = body["scope"]
    if scope == "per_entity":
        return f"-- {rid}\nSELECT * FROM policies WHERE {expr_sql(body['must_satisfy'])};"
    if scope == "portfolio_aggregate":
        agg = body["aggregate"]
        if agg["function"] == "weighted_avg":
            lhs = (
                f"SUM({col(agg['attr'])} * {col(agg['weight_attr'])}) / "
                f"NULLIF(SUM({col(agg['weight_attr'])}), 0)"
            )
        else:
            lhs = f"{agg['function'].upper()}({col(agg['attr'])})"
        return f"-- {rid}\nSELECT 1 FROM policies HAVING {constraint_sql(body['must_satisfy'], lhs)};"
    if scope == "portfolio_subset_share":
        total = f"SUM({col(body['share_of'])})"
        threshold = body["share_constraint"]["value"] / 100
        if "subset_groupby" in body and body["subset_groupby"] is not None:
            group_col = col(body["subset_groupby"])
            return (
                f"-- {rid}\nSELECT {group_col}, SUM({col(body['share_of'])}) / "
                f"NULLIF({total}, 0) AS share\nFROM policies\nGROUP BY {group_col}\n"
                f"HAVING share <= {threshold};"
            )
        subset = expr_sql(body["subset"])
        lhs = f"SUM(CASE WHEN {subset} THEN {col(body['share_of'])} ELSE 0 END) / NULLIF({total}, 0)"
        return f"-- {rid}\nSELECT 1 FROM policies HAVING {lhs} <= {threshold};"
    if scope == "fee":
        return f"-- {rid}\n-- Fee rule `{body['name']}` compiles to an event-gated fee function."
    raise ValueError(f"Unsupported scope: {scope}")


def main() -> None:
    blocks = []
    for path in sorted(OUTPUT_DIR.glob("sample*.json")):
        doc = Document.model_validate(json.loads(path.read_text(encoding="utf-8")))
        blocks.append(f"-- {doc.document_id}")
        for rule in doc.model_dump(mode="json")["rules"]:
            blocks.append(compile_rule(rule))
    COMPILED_PATH.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    print(f"Wrote {COMPILED_PATH}")


if __name__ == "__main__":
    main()
