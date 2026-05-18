# Claude skill: synthesize a CHANGELOG entry for a multi-PR back-port

Summarise a back-port of {n_prs} pull requests from `{source_repo}`
into one CHANGELOG entry for the downstream release on `{base_branch}`.
Treat the listed PRs as a single landed change.

## Output rules

- Prose only. No headings, no quotes, no code fences. One or two
  sentences.
- Imperative present tense ("Add support for X", "Fix Y", "Improve Z").
- Do NOT mention PR numbers, authors, file paths, or releasy-internal
  jargon — attribution is appended separately.

## What to include / drop

- DROP intra-group bug fixes (PR B fixes a bug PR A introduced in this
  same group): users never saw it.
- DROP refactors, internal cleanup, and test-only changes.
- For a feature group, lead with the feature; mention follow-ups only
  when they add user-visible capability or fix a production-observed
  bug.
- For a multi-PR bug fix, describe the user-facing fix once.

## Source PRs (cherry-pick order)

{pr_blocks}

## Now output the changelog entry.
