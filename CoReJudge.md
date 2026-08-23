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
6. Do not reject a correct partial bug description solely because it does not match
   every oracle field.
7. If the candidate describes multiple distinct bugs, evaluate each bug independently
   and set `match` to true when at least one of them correctly identifies the oracle
   bug. Other candidate bugs do not also need to match the oracle.
8. Set `match` to false only when none of the candidate bugs establishes the oracle
   bug's causal mechanism because every stated root cause is wrong, materially
   contradicts the oracle, or is too vague.

Candidate description:

```json
{{CANDIDATE_DESCRIPTION_JSON}}
```

Structured judge oracle:

```json
{{JUDGE_ORACLE_JSON}}
```

Return only one JSON object with exactly this shape(do not wrap in a Markdown fence ```json):

{
  "match": true,
  "reason": "Brief evidence-based explanation of the decision."
}
