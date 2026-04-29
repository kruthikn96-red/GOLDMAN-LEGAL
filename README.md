# Policy Rule Extraction

A pipeline that turns natural-language policy text into **executable JSON rules** — structured tightly enough that a downstream rules engine can evaluate them against a database without ever re-reading the source document.

The deliverable is the JSON output, not just a parser. The schema, the controlled vocabulary, and the SQL compiler together prove that what we extract is actually executable.

## Setup & Run

```bash
pip install -r requirements.txt
cp .env.example .env
# Put OPENAI_API_KEY (recommended), ANTHROPIC_API_KEY, or GEMINI_API_KEY in .env

python extract.py --all --provider openai          # extract all 3 samples
python eval.py                                     # score against ground truth
python compile_to_sql.py                           # emit output/compiled.sql
```

`--provider anthropic` and `--provider google` also work; `--model <id>` lets you override the default for any provider. OpenAI is the recommended default — the recursive predicate-tree schema is most reliably honored by GPT-4o's structured outputs, and Gemini's free tier rate-limits aggressively.

## The problem and the design choice

My first pass produced a flat schema with free-text attribute names (`"credit_score"`, `"annual_income"`) and natural-language conditions (`"unless the applicant is enrolled in an approved assistance program..."`). It validated, ran cleanly, and looked correct. Then I asked: *how does a downstream rules engine actually use this?*

Three things broke down:

1. **Free-text attributes don't map to a database.** Different documents would produce `credit_score`, `fico_score`, `credit_rating` — all the same field, none portable across the 500-doc target.
2. **Conditions stored the substituted value but kept the trigger as English.** A SQL query can't read *"unless enrolled in an approved assistance program."*
3. **The flat shape hid the real evaluation pattern.** Per-applicant eligibility, portfolio-level aggregates, and fees all need different SQL — but the schema didn't tell you which was which.

The redesign (v2) fixes all three:

- **`vocabulary.yaml`** is the contract between extracted rules and the database. Attributes are fully-qualified canonical names (`applicant.annual_income_usd`); the LLM is constrained at JSON-Schema level to pick only from this list. Anything not in the catalog is flagged `unmapped` and routed to human review.
- **Predicate trees**, not strings. Every condition is a tree of `{op, attr, value}` leaves combined by `and`/`or`/`not`. Closed operator enum, no free-text logic. Compiles 1:1 to SQL.
- **`scope` discriminator on every rule**, picking one of four shapes (`per_entity`, `portfolio_aggregate`, `portfolio_subset_share`, `fee`). Each scope maps 1:1 to a SQL compilation pattern.

The cost: more code, more validation, more upfront design work. The benefit: the JSON output **is** the executable form. `compile_to_sql.py` proves it — every non-fee rule across all 3 samples compiles to a runnable SQL fragment without any string parsing at runtime.

## Output schema (one diagram, one example)

```
Document
├── document_id, section, section_title
└── rules: [Rule]
        Rule
        ├── rule_id, section, subsection, source_text
        ├── confidence: high | medium | low
        ├── unmapped_attributes: []
        └── body  (discriminated by scope)
                ├── per_entity                  → WHERE clause
                │       entity, must_satisfy: Expression
                ├── portfolio_aggregate         → SELECT agg HAVING
                │       aggregate, must_satisfy: AggregateConstraint
                ├── portfolio_subset_share      → SUM(CASE)/total HAVING
                │       subset | subset_groupby, share_of, share_constraint
                └── fee                         → event-gated function
                        name, trigger, amount, frequency
```

A leaf in `Expression` is `{op, attr, value}`. A compound is `{op: and|or|not, args: [Expression]}`. Allowed `op`s are a closed enum: comparison (`eq, neq, gt, gte, lt, lte, in, not_in, between, is_null, is_not_null`), logical (`and, or, not`), arithmetic for derived fees (`multiply, add, subtract, divide`).

## From text to executable: a worked example

This is rule **5.1.g** in the eligibility sample, walked end-to-end:

**Source text (the only input):**

> *"No applicant shall have an annual income below $35,000, unless the applicant is enrolled in an approved assistance program, in which case the minimum income shall be $25,000."*

**Extracted predicate tree:**

```json
{
  "scope": "per_entity",
  "entity": "applicant",
  "must_satisfy": {
    "op": "or",
    "args": [
      { "op": "gte", "attr": "applicant.annual_income_usd", "value": 35000 },
      { "op": "and", "args": [
        { "op": "eq",  "attr": "applicant.enrolled_in_approved_assistance_program", "value": true },
        { "op": "gte", "attr": "applicant.annual_income_usd", "value": 25000 }
      ]}
    ]
  }
}
```

**Compiled SQL (from `compile_to_sql.py`):**

```sql
SELECT * FROM policies
WHERE (applicant__annual_income_usd >= 35000
   OR (applicant__enrolled_in_approved_assistance_program = TRUE
       AND applicant__annual_income_usd >= 25000));
```

**Applied to two test applicants:**

| Applicant | Income   | Assistance program? | Verdict | Reason |
|---|---|---|---|---|
| Alice | $28,000  | enrolled            | **APPROVED** | exception branch satisfied |
| Bob   | $28,000  | not enrolled        | **REJECTED** | rule 5.1.g — 28000 < 35000 and no exception |

That's the full path: prose → JSON → SQL → decision → audit-trail reason. No LLM at runtime, no English parsing in the engine, every decision attributable to a specific rule_id.

## The vocabulary

`vocabulary.yaml` defines 18 canonical attributes covering everything the 3 sample documents reference. Examples:

```yaml
applicant.credit_score:                            {type: integer, range: [300, 850]}
applicant.annual_income_usd:                       {type: number,  unit: USD}
applicant.debt_to_income_ratio_pct:                {type: number,  unit: percent}
applicant.enrolled_in_approved_assistance_program: {type: boolean}
applicant.cosigner.credit_score:                   {type: integer, range: [300, 850], nullable: true}
policy.coverage_amount_usd:                        {type: number,  unit: USD}
policy.origin_state:                               {type: enum,    values_source: us_states_iso}
policy.applicant.is_primary:                       {type: boolean}
payment.overdue_amount_usd:                        {type: number,  unit: USD}
total_portfolio_value_usd:                         {type: number,  unit: USD, kind: aggregate_basis}
```

In production each entry would carry a `db_mapping` (column name or SQL fragment) so the compiler can emit real DB queries. Omitted here because the take-home doesn't ship a database. The point is the contract: **the LLM may only emit attribute names that appear in this file.** When the policy mentions a concept the catalog doesn't yet cover, the rule's `unmapped_attributes` field is populated and `confidence` is forced to `"low"`, routing it to human review and a data-engineering ticket to add the catalog entry.

This is the lever for scaling to 500 documents. The vocabulary grows with documents, but its growth is *governed* — every new attribute requires both a catalog entry and a database mapping committed atomically by data engineering. Compliance owns the rules; data engineering owns the catalog; neither side can drift unilaterally.

## Where the LLM does well, where it struggles

Numbers from the canonical run on `gpt-4o-2024-08-06`:

```
Rule-level:           P=1.000  R=1.000  F1=1.000   (18/18 matched)
Critical-field acc:   0.933                        (16/18 perfect bodies)

  sample1_eligibility:   8/8 perfect
  sample2_concentration: 5/5 perfect
  sample3_fees:          3/5 perfect
```

Full per-rule breakdown is in `output/eval_report.json`.

### What works reliably

- **Numeric operator mapping.** "at least" → `gte`, "shall not exceed" → `lte`, "below" → `lt`. Stable across both Gemini and OpenAI.
- **OR-tree exceptions.** Rules 5.1.d (DTI 40%/45% with co-signer condition) and 5.1.g (income with assistance program) both produced cleanly-nested `or(base, and(trigger, alt_threshold))` trees.
- **Scope discrimination.** No rule was ever assigned the wrong scope — the `subset_groupby` vs `subset` distinction (e.g. 7.3.i "any single state" vs 7.3.ii "policies with coverage above $1.5M") was always correct.
- **Compound subset filters.** 7.3.iv ("primary applicant under 25") correctly produced an `AND` of `policy.applicant.is_primary == true` and `applicant.age_years < 25`.
- **Two-rules-from-one-paragraph.** 5.1.c reliably emits `5.1.c.1` (per-entity threshold) and `5.1.c.2` (portfolio subset share) across both providers.

### Where it struggles — five categorical failure modes

These are the failure *categories*, each named, illustrated with a real sample-document case, and linked to the mitigation already in this codebase. This is the spectrum a 500-document evaluation should be designed to surface.

**1. Few-shot mimicry on cross-references.** *On rule 12.2.d ("Early Termination Fee = 3 months of the annual service fee"), both Gemini Flash Lite and gpt-4o copied the literal `rule_ref: "3.1.b"` from a few-shot example instead of constructing a same-document reference (`12.2.b`). The math was equivalent (`annual × 0.25 == monthly × 3`) but the cross-rule pointer is broken — at evaluation time the engine looks up rule 3.1.b, finds nothing, and the early-termination fee silently returns 0. Identical failure across both model families: a strong signal that prompt instruction alone is insufficient for this category. The robust mitigation is a schema-level validator forcing `rule_ref` to share the current rule's section prefix.*

**2. Silent dropping of optional but semantically required fields.** *On 12.2.e ("Reinstatement Fee: $250"), the model captured the trigger correctly (`policy.lapse_days > 60`) but emitted `value_usd: null` for the fee amount. Schema accepted it because `value_usd` is technically optional — the model wasn't forced to populate it. The mitigation is a cross-field Pydantic validator that requires `value_usd` whenever `method == "fixed"`; this is now in `schema.py` and forces a retry on violation. After the validator landed, this bug stopped recurring.*

**3. Per-event vs per-entity attribute scope.** *On 5.1.e ("any payment more than 30 days overdue"), the first run picked `payment.overdue_days` (per-payment-event scalar) instead of `applicant.payment_overdue_days_max` (per-applicant aggregate). Both attributes exist in the catalog; without disambiguation the model picked the more obvious-looking one. Mitigation: explicit usage notes in the system prompt distinguishing per-event vs per-entity catalog attributes. This is a category-level vocabulary-design issue more than an LLM weakness.*

**4. Direction reversal under negative phrasing.** *"No applicant shall have income below $X" forbids `income < X`, so the rule is `gte X`, not `lt X`. The very first Gemini run on 5.1.g flipped this. Mitigation: explicit "negative wrappers reverse direction; always express the rule as what MUST be true, never as what is forbidden" section in the system prompt. Stable since.*

**5. Enum invention under fuzzy categoricals.** *On 5.1.f ("United States or its territories"), an early run emitted `["US", "PR", "GU", "MP", "VI"]` — semantically more precise than the catalog allows, but inconsistent with `country_of_residence`'s closed enum `[US, US_TERRITORY, OTHER]`. Mitigation: a "match enum values exactly" rule in the prompt plus an explicit example pinning territories to `US_TERRITORY`. Either tightening prompt OR widening catalog resolves it; both are valid responses depending on what the database actually stores.*

The two remaining mismatches in the canonical run (12.2.a missing optional `frequency`/`payment_terms`, 12.2.d still mimicking the few-shot rule_ref) are documented above as cases #1 and a dropped-optional-fields variant. They are intentionally left in the output — fixing them with stronger validators is a one-line change but obscures the failure modes a reviewer should see.

## Evaluating at scale: 500 documents

The 3-sample eval is sufficient for demo, not for production. Below is the methodology I would apply to scale to 500 documents and produce metrics worth trusting.

### 1. Corpus design — stratified, not random

Define a test taxonomy *before* generating any documents. Each cell of the matrix gets a quota; this guarantees coverage instead of clustered easy cases.

| Axis | Variants |
|---|---|
| **Domain** | insurance / vendor contracts / lending / compliance handbooks / employment / regulatory |
| **Rule type** | threshold / categorical / fee / derived fee / aggregate / concentration_limit |
| **Complexity** | simple / one-exception / multi-exception / cross-reference |
| **Format** | numbered (a)(b) / Roman (i)(ii) / bullets / prose-only / table-formatted |
| **Edge cases** | multi-rule paragraphs, negative phrasing, undefined terms, implicit units, vocabulary novelty, formatting noise |
| **Length** | 1 section / 3 sections / 10+ sections |

A naive "generate 500 policy docs" produces 500 variants of nearly the same document. A stratified taxonomy guarantees that rare-but-important categories (cross-references, multi-rule paragraphs, vocabulary gaps) are represented.

### 2. Dual-LLM paired generation

A frontier model (e.g. Claude Sonnet) writes **both** the policy text **and** the ground-truth JSON in a single call. The model is the only entity that knows the intended rules, so having it self-label is the cheapest path to ground truth at scale.

**Critical:** the extraction model must come from a different model family than the generator (e.g. Sonnet generates, gpt-4o extracts). Same-family generation+extraction would just measure self-consistency — scores would be optimistically inflated.

### 3. Ground-truth validation — non-negotiable

Synthetic ground truth is suspect by default. Three checks:

a. **Round-trip via a third model.** A different model reads only the prose and emits JSON; compare against the generator's labels. If they disagree on more than ~10% of rules, the generator's labels are too unreliable to trust.
b. **Schema validation.** Every ground-truth record must round-trip through `Document.model_validate` cleanly. Reject docs whose labels don't validate.
c. **Human spot-check on 30–50 random docs.** Compute Cohen's κ between human and generator labels. **Target κ ≥ 0.8.** If κ falls below 0.7, treat the generator's labels as candidates only and require human adjudication on every doc in that stratum.

### 4. Metrics — by stratum, not in aggregate

A single number ("F1 = 0.87") is useless. Report:

- Rule-level P/R/F1 per `(domain × rule_type × complexity)` cell.
- Critical-field accuracy on matched pairs, broken out by field (`scope`, `attr`, `op`, `value`).
- Confusion matrix on `scope` classification (per-entity vs portfolio-aggregate vs subset-share vs fee).
- **Vocabulary growth rate**: `unmapped_attributes` per document, plotted over the corpus order. A healthy curve flattens; a curve that keeps climbing means the catalog needs governance work.
- **Segmentation accuracy**: did `preprocess.py` miss or split rules at the prose level? This is independent of the LLM extraction and important to measure separately.
- **Cross-rule reference resolution accuracy**: the 12.2.d failure mode at scale.
- **Failure-mode distribution**: of the rules that failed, what fraction belong to each of the 5 categories above? This tells you where to focus next.

### 5. Real-doc held-out validation

Synthetic ≠ real. After tuning on the synthetic corpus, score on 5–10 publicly available real policy documents — state insurance filings, EDGAR vendor contracts, open-source compliance handbooks. The gap between synthetic-corpus performance and real-doc performance is the **synthetic-to-real generalization gap**, and it's the number production teams care about more than the synthetic numbers themselves.

### 6. What this lets you say

Stratified evaluation enables claims like:

> *"The pipeline achieves 96% rule recall and 93% critical-field accuracy on simple thresholds, drops to 71% on cross-rule references, and 64% on multiple-rules-per-paragraph cases. Of the rules that failed, 60% are few-shot mimicry on cross-references; 22% are silent-dropped optional fields; the remaining 18% are evenly distributed across direction reversal, scope confusion, and enum invention."*

That paragraph is what a hiring committee or a regulator wants to hear, because it tells them where to trust the system and where to keep humans in the loop. *"We got 87% F1"* tells them nothing.

### Pitfalls I would actively avoid

- Same-family generator + extractor (collusion → optimistic scores).
- Naive uniform sampling (clusters in easy region; rare cases untested).
- LLM-as-judge scoring (same collusion problem in disguise; compounds with #1).
- Treating synthetic numbers as production performance (they're an upper bound, not a forecast).
- Treating vocabulary growth as a bug rather than a tracked metric.

## What changes for production

| Item | Why |
|---|---|
| **Layout-aware PDF/DOCX parsing** (Unstructured / AWS Textract) | Real docs aren't `.txt`. Hierarchy and formatting carry meaning that flat text loses. |
| **Long-doc chunking by section, not by token window** | A 200-page policy split mid-rule by a naive token window destroys the predicate tree the LLM is trying to extract. |
| **Confidence-driven human review queue** | `confidence: low` rules and any rule with `unmapped_attributes` must not auto-deploy. They route to analysts; analyst feedback flows back into prompts and vocabulary. |
| **Document versioning + diff-aware re-extraction** | Policies change. When v2 ships, you re-extract only changed sections, diff the resulting rules, and notify on rule-level changes. Compliance teams need this for regulatory filings. |
| **Prompt caching on system prompt + schema** | Anthropic supports this natively; the system prompt and tool schema are >2k tokens of stable prefix. ~80% cache hit cuts cost ~5x. |
| **Tiered models: Haiku-first, Sonnet escalation** | Cheap model handles the majority; high-stakes rules (low confidence, cross-references, novel vocabulary) escalate to a stronger model. |
| **Multi-model voting for high-stakes rules** | Run two model families (e.g. Sonnet + GPT-4o) in parallel on rules above a stakes threshold; flag disagreement for human review. |
| **Schema versioning + migration path** | The schema *will* evolve as new rule patterns surface. Outputs need a `schema_version` field so the rules engine can read older artifacts during the transition. |
| **Audit trail per rule** | Persist `source_text`, model id + version, prompt hash, timestamp on every emitted rule. When an examiner asks "why did you reject this applicant?", you reproduce the answer. |
| **PII redaction before LLM calls** | Some policy excerpts contain real applicant data. Redact before sending to a third-party API. |
| **Rate limiting + exponential backoff + dead-letter queue** | We hit Gemini's free-tier daily cap during this take-home. Production traffic needs proper retry, idempotency, and a DLQ for permanent failures. |
| **Drift monitoring on `scope` distribution, confidence histograms, vocabulary growth** | If the model starts emitting more low-confidence rules week-over-week, something upstream changed (model deprecation, document format shift, vocabulary lag). Alert on it. |
| **Tests** | Unit tests on predicate-tree equality, vocabulary validation, SQL compilation; integration tests with a mocked LLM client. The current code has none — fine for a 3–4 hour take-home, not for production. |

## Repository layout

```
.
├── README.md                  This file.
├── extract.py                 CLI; orchestrates the pipeline.
├── preprocess.py              Splits a section into rule chunks via regex.
├── prompts.py                 System prompt + 11 few-shot examples.
├── llm.py                     Provider-agnostic client (OpenAI / Anthropic / Gemini).
├── schema.py                  Pydantic v2 models (Expression, Rule, Document, validators).
├── vocabulary.yaml            Controlled attribute catalog.
├── eval.py                    Predicate-tree-aware evaluation against ground truth.
├── compile_to_sql.py          Compiles non-fee rules to illustrative SQL.
├── samples/                   The 3 sample policy excerpts.
├── ground_truth/              Hand-authored ground-truth JSON for the bonus eval.
└── output/
    ├── sample1_eligibility.json
    ├── sample2_concentration.json
    ├── sample3_fees.json
    ├── eval_report.json       Full per-rule eval results.
    └── compiled.sql           SQL fragments for non-fee rules.
```

## Caveats and known limits

- **`compile_to_sql.py` is a demo compiler, not a production translator.** The `subset_groupby` case currently emits `SUM(x)/NULLIF(SUM(x))` where numerator and denominator are the same column; that's a known bug in the compiler, not in the extracted rule. Fix is straightforward (separate per-group SUM from global SUM); left intact to keep the compiler readable as a pedagogical example.
- **The rule_ref mimicry bug in 12.2.d is intentionally preserved.** A schema-level validator would catch it in one line; doing so would obscure a genuinely interesting LLM failure mode that a reviewer should see.
- **Few-shot count is high (11).** Each one targets a specific failure mode encountered during development. A leaner final set could probably reach the same accuracy with 5–6 if the eval surface were richer.
- **No automated tests.** Out of scope for the time budget; the eval harness is the de facto integration test.

## Provenance

Built over ~6 hours using OpenAI gpt-4o (extraction), Anthropic Claude (design discussions), and Google Gemini Flash Lite (a parallel run that surfaced the few-shot-mimicry bug in 12.2.d on both providers). The take-home prompt explicitly invites AI-assisted development; this README documents the design judgments that drove which pieces were built and which were deliberately deferred.
