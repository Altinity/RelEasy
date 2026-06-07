# Claude skill: rebuild a PR port-dependency graph from member feedback

Maintain the port dependency graph for base branch `{base_branch}`. A graph
is a set of *units* (a unit = one or more PRs cherry-picked together) with
`depends_on` edges (`A depends_on B` ⇒ port B first).

Trusted members left comments asking for changes. Read their intent, apply it,
and output the **new graph**. Preserve anything the comments don't touch;
ignore non-actionable comments (questions, 👍). Allowed actions:

- **Add** a PR ("also port #2000").
- **Veto** a PR ("drop #1010") — list under `exclude`, omit from every unit.
- **Regroup** — merge PRs into one atomic unit, or split a unit.
- **Reorder** — add/remove `depends_on` edges.

## Current graph

{current_graph_block}

## PRs already in the graph

{candidate_pr_list}

Reference these by URL; introduce a new PR URL only when a comment asks to add it.

## Member comments (trusted; newest last)

Each comment has a handle like `[C1]`. Report which you applied via `addressed`.

{comments_block}

## Output

A short rationale, then the **complete** new graph as one fenced YAML block.
The YAML is mandatory — never reply with only a "changes made" summary or a
diff. It *replaces* the graph, so emit every unit that should remain (even
when one comment changed one unit):

```yaml
units:
  - id: <unit-id>            # keep prior ids stable
    prs:
      - <pr-url>             # multiple ⇒ atomic group; list in apply order
                             # (prerequisite first — this IS the cherry-pick seq)
    depends_on: [<unit-id>]  # omit if none
exclude:                      # omit if nothing vetoed
  - url: <pr-url>
    reason: <short reason>
addressed: [C1, C3]           # comment handles you applied; omit the rest
```

Rules: every `prs` URL is a real GitHub PR URL (from above, or explicitly
requested); each PR is in exactly one unit *or* `exclude`, never both;
`depends_on` ids must exist in this block; no cycles. List under `addressed`
only the comment handles you actually applied — leave out questions, 👍, and
requests you ignored or disagreed with, so they stay visible for a human.

## Respond now.
