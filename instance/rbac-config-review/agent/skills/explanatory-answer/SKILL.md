---
name: explanatory-answer
description: Compose a finding's or ledger re-check's explanatory text (claim, evidence, impact, trigger, suggested_fix, status_note) as a structured, evidenced explanation instead of vague prose. Use this whenever writing those fields in workflows/review-only, before handing a finding to review_evidence.py.
---

# Explanatory Answer

`review_evidence.py`'s structural validation can confirm a finding's
evidence ref is *pinned* to something real — it cannot tell whether the
*writing* is actually useful, or generic filler that happens to pass schema
validation. This skill is the writing discipline for every explanatory field
in a finding or a ledger re-check.

## When to Use This Skill

- Composing a new finding's `claim`, `trigger`, `impact`, `evidence.explanation`,
  or `suggested_fix` (workflows/review-only/AGENTS.md step 3)
- Composing a ledger re-check's `explanation` for an `addressed`/`still_open`/
  `uncertain` verification (step 3, second JSON file)
- Writing the `Status:` / `Fix evidence:` lines `render_ledger` carries
  forward into the posted review

## Structure

Four parts, mapped directly onto the finding schema — this is not a
separate format, it is how to fill out the fields that already exist well:

### 1. Claim — the direct statement

`claim` is the one- or two-sentence assertion itself: "X will do Y." Not "X
might be worth checking" or "X could potentially cause issues" — state the
specific failure, or don't propose the finding.

### 2. Evidence — cited, pinned, not recalled

`evidence.explanation` + `evidence.refs` must point at something actually
read in `manifest.json`'s pinned sources this cycle, not a remembered
impression of what a file "probably" does:

- Quote or closely paraphrase the real line(s) at the ref's file/line.
- A `cross_repo` claim needs a concrete related-repo reference — normally a
  `consumer_usages` entry from `impact.json` — never the `review-context.yaml`
  relationship declaration alone (the declaration says a relationship
  *exists*, not that *this* change touches it).
- If the available pinned sources don't actually establish the claim, don't
  propose the finding — `impact.json`'s matches are leads, not proof.

### 3. Impact — the concrete consequence

`impact` is what actually goes wrong, for someone specific, not a
severity-adjective:

- Bad: "This could cause issues for dependent services."
- Good: "HBI's `client.py` still sends `inventory.read_hosts` on every
  host-read request; after this PR, RBAC no longer recognizes that
  permission string, so those requests get rejected with 403."
- If the blast radius is uncertain, say what's uncertain rather than
  rounding up to a confident-sounding guess.

### 4. Trigger — the concrete example

`trigger` is the specific input or code path that exercises the problem —
the example, in finding form:

- "HBI calls `check_permission("inventory.read_hosts")`" is a trigger.
  "In some cases this might not work" is not.
- For a ledger re-check claiming `"addressed"`, the equivalent is the fix
  evidence itself: cite the exact line that now handles the case the
  original trigger exercised — not "this looks fixed."

## Worked Example

A finding written well, from this workflow's own documentation
(`WORKFLOW.md`'s "Example comments"):

```json
{
  "id": "F1", "category": "cross_repo",
  "claim": "Old permission name disappears while HBI still sends it",
  "trigger": "HBI calls check_permission(\"inventory.read_hosts\") on every host-read request",
  "impact": "HBI requests to the inventory API are rejected with 403 after this PR merges",
  "evidence": {
    "explanation": "HBI's client.py still sends the old permission string",
    "refs": [{"repo": "RedHatInsights/insights-host-inventory", "sha": "c3c3...", "file": "client.py", "line": 1}]
  },
  "suggested_fix": "Keep inventory.read_hosts as an accepted alias until HBI migrates"
}
```

Each field answers a different question a reviewer would actually ask:
*what's wrong* (claim), *when does it happen* (trigger), *so what*
(impact), *how do you know* (evidence), *what do I do about it*
(suggested_fix). None of them restate another field in different words.

## Anti-patterns

- A `claim` that's really a vague concern ("might be worth double-checking")
- `evidence.explanation` that doesn't match what the pinned `refs` actually
  say, or restates the claim instead of citing something
- `impact` as a severity label with no concrete scenario attached
- `trigger` that's a restatement of the claim instead of a specific input
- A `still_open`/`uncertain` `status_note` that doesn't say what was checked
  ("couldn't confirm" without saying what source was consulted and found
  insufficient)
