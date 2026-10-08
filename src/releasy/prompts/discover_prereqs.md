# Claude skill: confirm / refine PR prerequisite candidates from a conflict

Decide which of a *given* candidate list of older un-ported PRs are real
prerequisites for one trial-picked unit. Confirm or drop only — never
add candidates not in the list.

## Inputs

- **Trial-picked unit:** `{unit_id}` (source PR: {source_pr_url} — "{source_pr_title}")
- **Target branch:** `{base_branch}`
- **Conflict files** (left in the worktree by `git cherry-pick -m 1`):

{conflict_files}

- **Candidate prerequisites** (older un-ported units whose commits
  touched the conflict files on the source branch):

{candidate_deps_block}

## What counts as a prerequisite

Unit `D` is a prerequisite of `{unit_id}` iff the conflict on the
listed files is **caused by** `D` being absent from `{base_branch}`:
`D` introduced or moved code that `{unit_id}`'s diff builds on, and
without `D` the conflict cannot be resolved into something equivalent
to what the source branch carries.

NOT a prerequisite:

- merely touches the same file, conflict is over an independent region
- conflict is whitespace / formatting / comment drift
- conflict is caused by upstream refactors not represented in candidates

## Output contract — strict

Exactly one of these forms. No preamble, no commentary, nothing else.

Confirming one or more:

```
MISSING_PREREQS: <pr_url_1> <pr_url_2> ...
REASON: <one line, <200 chars>
```

URLs must be taken verbatim from the candidate list. Space-separated.
For a group row listing several URLs, naming **any one** marks the
whole group.

None apply:

```
MISSING_PREREQS:
```

(empty list, no REASON line)

## Respond now.
