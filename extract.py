"""CLI entrypoint for the policy rule extraction pipeline.

Usage:
    python extract.py samples/sample1_eligibility.txt -o output/sample1.json
    python extract.py --all                   # all three samples in samples/
    python extract.py --provider google ...   # override LLM provider
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError
import yaml

from llm import LLMResponse, get_client
from preprocess import RuleChunk, split_document
from schema import Document, LogicalExpr, Predicate, Rule, extraction_payload_schema

SAMPLES_DIR = Path("samples")
OUTPUT_DIR = Path("output")
VOCABULARY_PATH = Path("vocabulary.yaml")


def load_vocabulary(path: Path = VOCABULARY_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping of attribute names.")
    return data


def extract_rules_for_chunk(
    client, chunk: RuleChunk, schema: dict, allowed_attrs: list[str]
) -> tuple[list[Rule], LLMResponse]:
    """Call the LLM on one rule chunk; validate and return Rule objects.

    Retries once if validation fails, including the validation error in the
    follow-up prompt.
    """
    chunk_payload = {
        "rule_id": chunk.rule_id,
        "section": chunk.section,
        "section_title": chunk.section_title,
        "subsection": chunk.subsection,
        "source_text": chunk.source_text,
    }

    response = client.extract(chunk_payload, schema, allowed_attrs=allowed_attrs)
    raw_rules = response.payload.get("rules", [])

    rules, errors = _validate_rules(raw_rules, chunk, allowed_attrs)
    if errors and not rules:
        # Retry once with the validation error appended
        retry_chunk = dict(chunk_payload)
        retry_chunk["_validation_error"] = errors
        response = client.extract(retry_chunk, schema, allowed_attrs=allowed_attrs)
        raw_rules = response.payload.get("rules", [])
        rules, errors = _validate_rules(raw_rules, chunk, allowed_attrs)

    if errors:
        sys.stderr.write(
            f"[warn] {chunk.rule_id}: dropped {len(errors)} invalid rule(s):\n"
        )
        for err in errors:
            sys.stderr.write(f"        {err}\n")

    return rules, response


def _validate_rules(
    raw_rules: list[dict], chunk: RuleChunk, allowed_attrs: list[str]
) -> tuple[list[Rule], list[str]]:
    rules: list[Rule] = []
    errors: list[str] = []
    allowed = set(allowed_attrs)
    for raw in raw_rules:
        # Backstop: ensure rule_id / section / subsection match the chunk,
        # even if the model wandered.
        raw.setdefault("rule_id", chunk.rule_id)
        raw.setdefault("section", chunk.section)
        raw.setdefault("section_title", chunk.section_title)
        raw.setdefault("subsection", chunk.subsection)
        raw.setdefault("source_text", chunk.source_text)
        try:
            rule = Rule.model_validate(raw)
            rules.append(_apply_vocabulary_validation(rule, allowed))
        except ValidationError as e:
            errors.append(f"{e.error_count()} validation errors: {e.errors()[:2]}")
    return rules, errors


def _iter_expr_attrs(expr) -> list[str]:
    if isinstance(expr, Predicate):
        return [expr.attr]
    if isinstance(expr, LogicalExpr):
        attrs: list[str] = []
        for arg in expr.args:
            attrs.extend(_iter_expr_attrs(arg))
        return attrs
    return []


def _rule_attrs(rule: Rule) -> list[str]:
    body = rule.body
    attrs: list[str] = []
    if hasattr(body, "must_satisfy"):
        attrs.extend(_iter_expr_attrs(body.must_satisfy))
    if hasattr(body, "subset") and body.subset is not None:
        attrs.extend(_iter_expr_attrs(body.subset))
    if hasattr(body, "subset_groupby") and body.subset_groupby is not None:
        attrs.append(body.subset_groupby)
    if hasattr(body, "share_of"):
        attrs.append(body.share_of)
    if hasattr(body, "aggregate"):
        attrs.append(body.aggregate.attr)
        if body.aggregate.weight_attr:
            attrs.append(body.aggregate.weight_attr)
    if hasattr(body, "trigger") and body.trigger.when is not None:
        attrs.extend(_iter_expr_attrs(body.trigger.when))
    if hasattr(body, "amount") and body.amount.of_attr is not None:
        attrs.append(body.amount.of_attr)
    return attrs


def _apply_vocabulary_validation(rule: Rule, allowed_attrs: set[str]) -> Rule:
    unmapped = sorted({attr for attr in _rule_attrs(rule) if attr not in allowed_attrs})
    if not unmapped:
        return rule
    existing = set(rule.unmapped_attributes)
    rule.unmapped_attributes = sorted(existing | set(unmapped))
    rule.confidence = "low"
    return rule


def extract_document(
    text: str, document_id: str, client, allowed_attrs: list[str]
) -> Document:
    section, section_title, chunks = split_document(text)
    schema = extraction_payload_schema()

    all_rules: list[Rule] = []
    for chunk in chunks:
        rules, _ = extract_rules_for_chunk(client, chunk, schema, allowed_attrs)
        all_rules.extend(rules)
        print(f"  {chunk.rule_id}: {len(rules)} rule(s) extracted", file=sys.stderr)

    return Document(
        document_id=document_id,
        section=section,
        section_title=section_title,
        rules=all_rules,
    )


def process_file(input_path: Path, output_path: Path, client, allowed_attrs: list[str]) -> None:
    print(f"\n[{input_path.name}]", file=sys.stderr)
    text = input_path.read_text(encoding="utf-8")
    doc = extract_document(
        text, document_id=input_path.stem, client=client, allowed_attrs=allowed_attrs
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    print(f"  -> wrote {len(doc.rules)} rules to {output_path}", file=sys.stderr)


def main():
    load_dotenv()

    p = argparse.ArgumentParser(description="Extract structured rules from policy text.")
    p.add_argument("input", nargs="?", help="Path to a sample .txt file.")
    p.add_argument("-o", "--output", help="Output JSON path (default: output/<stem>.json).")
    p.add_argument("--all", action="store_true", help="Process every .txt file in samples/.")
    p.add_argument(
        "--provider",
        default=None,
        help="LLM provider: 'anthropic' (default), 'openai', 'google', or 'ollama'.",
    )
    p.add_argument("--model", default=None, help="Override model id.")
    args = p.parse_args()

    client = get_client(provider=args.provider, model=args.model)
    vocabulary = load_vocabulary()
    allowed_attrs = sorted(vocabulary.keys())

    if args.all:
        if not SAMPLES_DIR.is_dir():
            p.error(f"{SAMPLES_DIR} not found.")
        for sample in sorted(SAMPLES_DIR.glob("*.txt")):
            out = OUTPUT_DIR / f"{sample.stem}.json"
            process_file(sample, out, client, allowed_attrs)
    else:
        if not args.input:
            p.error("Provide an input file or use --all.")
        in_path = Path(args.input)
        out_path = Path(args.output) if args.output else OUTPUT_DIR / f"{in_path.stem}.json"
        process_file(in_path, out_path, client, allowed_attrs)


if __name__ == "__main__":
    main()
