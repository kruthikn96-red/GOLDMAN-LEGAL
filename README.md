# Policy Rule Extraction

This project turns natural-language policy text into structured JSON rules that a downstream system can validate, review, compile, and evaluate.

The main idea is simple:

```text
Legal / policy text
        |
        v
Split into rule-sized chunks
        |
        v
LLM extracts structured rules
        |
        v
Pydantic schema validates the output
        |
        v
JSON rules can be compiled to SQL or reviewed by humans
```

The LLM handles language understanding. The code around it handles structure, validation, vocabulary control, evaluation, and auditability.

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# Add one API key: OPENAI_API_KEY, ANTHROPIC_API_KEY, or GEMINI_API_KEY

python extract.py --all --provider openai
python eval.py
python compile_to_sql.py
```

Outputs are written to:

```text
output/sample1_eligibility.json
output/sample2_concentration.json
output/sample3_fees.json
output/eval_report.json
output/compiled.sql
```

You can also run one sample:

```bash
python extract.py samples/sample1_eligibility.txt \
  --provider openai \
  -o output/sample1_eligibility.json
```

## Local Model Option

The pipeline also supports Ollama for local experiments:

```bash
ollama list
python extract.py samples/sample1_eligibility.txt \
  --provider ollama \
  --model qwen2.5:3b \
  -o output/sample1_eligibility.local.json
```

Local models are useful for privacy and cost experiments, but they are slower and less reliable at strict JSON. Cloud models are better for this specific structured extraction task.

## Workflow

```text
+-----------------------------+
| 1. Raw samples              |
| samples/*.txt               |
+--------------+--------------+
               |
               v
+-----------------------------+
| 2. Preprocess               |
| preprocess.py               |
| find section + subsection   |
+--------------+--------------+
               |
               v
+-----------------------------+
| 3. LLM extraction           |
| extract.py + llm.py         |
| prompts.py                  |
+--------------+--------------+
               |
               v
+-----------------------------+
| 4. Validate + govern        |
| schema.py                   |
| vocabulary.yaml             |
+--------------+--------------+
               |
               v
+-----------------------------+
| 5. JSON rules               |
| output/*.json               |
+--------------+--------------+
               |
       +-------+-------+
       |               |
       v               v
+-------------+  +----------------+
| SQL proof   |  | Evaluation     |
| compile_    |  | eval.py        |
| to_sql.py   |  |                |
+-------------+  +----------------+
```

## What Each File Does

| File | Purpose |
|---|---|
| `samples/*.txt` | The three policy excerpts from the prompt. |
| `preprocess.py` | Splits a section into subsection chunks like `5.1.a`, `5.1.b`, etc. |
| `prompts.py` | Tells the LLM how to map legal language into the JSON schema. |
| `llm.py` | Hides provider differences for OpenAI, Anthropic, Gemini, and Ollama. |
| `schema.py` | Defines and validates the structured rule objects. |
| `vocabulary.yaml` | Lists the only allowed database-style attributes. |
| `extract.py` | Orchestrates splitting, LLM calls, validation, retries, and output writing. |
| `compile_to_sql.py` | Converts non-fee JSON rules into SQL-like checks. |
| `eval.py` | Compares extracted JSON against hand-authored ground truth. |

## Why This Design

The task is not just to make JSON that looks correct. The output should be useful to a business system.

A raw sentence like this:

```text
Each applicant must have a credit score of at least 680.
```

becomes:

```json
{
  "scope": "per_entity",
  "entity": "applicant",
  "must_satisfy": {
    "op": "gte",
    "attr": "applicant.credit_score",
    "value": 680
  }
}
```

That structure can be compiled to SQL:

```sql
WHERE applicant__credit_score >= 680
```

This is why the project uses an intermediate JSON rule format instead of asking the LLM to generate SQL directly. JSON is easier to validate, diff, review, version, and reuse across different systems.

## Schema Design

`schema.py` is the contract between the LLM and the rest of the pipeline.

Each rule has:

```text
rule_id
section
subsection
source_text
confidence
unmapped_attributes
body
```

The `body.scope` decides how the rule should be executed:

| Scope | Used for | Example |
|---|---|---|
| `per_entity` | Row-level applicant or policy checks | applicant credit score >= 680 |
| `portfolio_aggregate` | Whole-portfolio calculations | weighted average credit score >= 720 |
| `portfolio_subset_share` | Concentration limits | no more than 25% from one state |
| `fee` | Event-triggered charges | late fee after 15 overdue days |

This matters because different rule types become different execution patterns. A simple applicant rule becomes a `WHERE` clause. A portfolio rule becomes an aggregate or `HAVING` check. A fee rule becomes event-triggered billing logic.

## Controlled Vocabulary

The LLM may only use attributes from `vocabulary.yaml`, such as:

```text
applicant.credit_score
applicant.annual_income_usd
policy.coverage_amount_usd
policy.origin_state
payment.overdue_days
total_portfolio_value_usd
```

This prevents the model from inventing fields like `fico_score`, `income`, or `coverage`.

If the model uses an attribute that is not in the vocabulary, `extract.py` marks the rule as low-confidence and records it in `unmapped_attributes`.

## What The LLM Does Well

The LLM is good at semantic interpretation:

- mapping phrases like "at least" to `gte`
- mapping "shall not exceed" to `lte`
- building `and` / `or` trees for exceptions
- identifying when one paragraph contains multiple rules
- recognizing fee triggers and percentage fee formulas

Example from sample `5.1.c`: one paragraph produces two rules:

```text
5.1.c.1 -> applicant credit score >= 680
5.1.c.2 -> portfolio share of credit scores 680-700 <= 15%
```

That is hard to do with only regex.

## Where The LLM Struggles

The model can still make subtle mistakes:

- **Wrong attribute choice:** using `payment.overdue_days` when the eligibility rule needs `applicant.payment_overdue_days_max`.
- **Direction reversal:** reading "No applicant shall have income below $35,000" as `income < 35000` instead of `income >= 35000`.
- **Enum invention:** outputting `PR`, `GU`, or `VI` instead of the allowed value `US_TERRITORY`.
- **Few-shot mimicry:** copying a rule id from an example, such as `3.1.b`, instead of creating the correct same-document reference.
- **Dropped required details:** omitting a fee amount or payment term.

The project handles these with prompt instructions, Pydantic validators, vocabulary checks, retries, and evaluation.

## Prompt Size And Scaling

The current prompt is intentionally rich because this is a small take-home with only three documents. It includes:

```text
system instructions
controlled vocabulary
operator rules
scope rules
few-shot examples
the current subsection
```

That is roughly 6k tokens per LLM call. Since the pipeline calls the LLM once per subsection, scaling this naively can get expensive.

For example:

```text
50,000 documents
~6 subsections per document
= ~300,000 LLM calls
```

At production scale I would reduce cost and latency with:

- prompt caching for the stable system prompt and examples
- retrieving only the few-shot examples relevant to the current rule
- using cheaper models for simple rules and stronger models for hard rules
- batch processing for non-urgent extraction jobs
- section-level extraction for short/simple sections
- moving more constraints into validators instead of prompt text

## Evaluation

This repo includes hand-authored ground truth in `ground_truth/`.

Run:

```bash
python eval.py
```

The eval reports:

```text
rule-level precision / recall / F1
critical field accuracy
missing rules
extra rules
scope-level breakdown
body mismatches
```

The important point is that evaluation should check more than "did we output JSON?" It should check whether the extracted rule has the right scope, operator, attribute, value, and logical structure.

## How I Would Evaluate 500 Documents

For 500 documents, I would not report one global accuracy number only.

I would build a stratified evaluation set across:

- document domain: insurance, lending, vendor contracts, compliance handbooks
- rule type: threshold, exception, fee, aggregate, concentration limit
- complexity: simple, multi-condition, cross-reference, multi-rule paragraph
- format: numbered clauses, Roman numerals, bullets, tables, prose-only

Then I would measure:

- rule recall: did we find every rule?
- field accuracy: did we get `scope`, `attr`, `op`, and `value` right?
- vocabulary growth: how often do new unmapped attributes appear?
- failure-mode distribution: which mistakes happen most often?
- synthetic-to-real gap: how well does it work on real public documents after tuning on synthetic ones?

## Production Improvements

For production I would add:

- layout-aware PDF/DOCX parsing instead of plain text only
- prompt, schema, vocabulary, and model versioning
- human review for low-confidence or unmapped rules
- stronger validation for cross-rule references
- async batch extraction with retries and dead-letter queues
- monitoring for drift in confidence, scope distribution, and unmapped attributes
- a real SQL/rules-engine compiler with database mappings
- a rule approval workflow before deploying extracted rules

## Known Limits

- `compile_to_sql.py` is a proof-of-concept compiler, not a production database integration.
- The prompt has many few-shot examples; this improves sample accuracy but should be optimized for scale.
- Local Ollama models can run this pipeline, but they are slower and less reliable for strict JSON.
- The system should route low-confidence rules to humans instead of auto-deploying them.

## Repository Layout

```text
.
├── extract.py
├── preprocess.py
├── prompts.py
├── llm.py
├── schema.py
├── vocabulary.yaml
├── compile_to_sql.py
├── eval.py
├── samples/
├── ground_truth/
└── output/
```
