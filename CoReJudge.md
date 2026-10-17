# Semantic evaluation

Judge according to these rules:

1. Judge whether the candidate bug description is semantically correct.
2. Function localization is scored separately and is not part of this decision.
3. Treat wording differences as irrelevant, but do not infer facts that the
   candidate did not state.
4. The structured judge oracle has no required field set. Treat its fields as
   evidence, not as a checklist that the candidate must reproduce.
5. Set `match` to true when the candidate correctly identifies the root cause, even
   if the rest of the description only partially covers or omits oracle details such
   as the symptom, impact, or trigger condition.
6. Evaluate each item in `bugs` independently. Set `match` to true if at least one
   identifies the oracle bug; never combine claims across items to construct a
   matching root cause.
7. Set `match` to false only when none of the candidate bugs establishes the oracle
   bug's causal mechanism because every stated root cause is wrong, materially
   contradicts the oracle, or is too vague.

Candidate bugs (`bugs: []` means no candidate and must yield `match: false`):

```json
{{CANDIDATE_DESCRIPTION_JSON}}
```

Structured judge oracle:

```json
{{JUDGE_ORACLE_JSON}}
```

Return only one JSON object with this exact shape, without a Markdown fence:

{
  "match": true,
  "reason": "Brief evidence-based explanation of the decision."
}
