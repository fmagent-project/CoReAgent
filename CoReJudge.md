# Task: Match a Candidate Bug to the Ground Truth

Determine whether the candidate bug below describes the same underlying bug as
the ground truth. Return `match: true` only when the candidate's stated cause
and the ground truth's cause are semantically equivalent. The two descriptions
may use different terminology or levels of detail.

Use the ground truth as evidence about the target bug, not as a list of fields
that the candidate must reproduce. Compare the actual claims in the candidate
with the ground truth, including the cause and relevant conditions or behavior
when provided. Do not add unstated claims to the candidate or assume that two
bugs are the same merely because they affect the same file, function, or
symptom.

Set `match` to `false` if the candidate describes a different cause, materially
contradicts the ground truth, or gives only a symptom or vague suspicion that
does not identify the ground-truth bug. A missing detail does not make a
candidate wrong when its stated cause is already specific enough to identify
the same bug.

Evaluate only the candidate bug shown below against the ground truth.

Candidate bug:

```json
{{CANDIDATE_DESCRIPTION_JSON}}
```

Ground truth:

```json
{{JUDGE_ORACLE_JSON}}
```

Return only one JSON object with this exact shape, without a Markdown fence.
Set `reason` to a brief explanation that names the candidate's relevant claim
and the corresponding ground-truth evidence:

{
  "match": true,
  "reason": "Brief evidence-based explanation of the decision."
}
