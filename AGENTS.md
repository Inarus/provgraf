# Working with this bank as an agent

This repository is a bank of verified facts. The point of it is that a number you find here can be
traced to the document or the person it came from, and that nothing enters it silently. That
constraint applies to you.

## The one rule

**Nothing enters the bank without provenance and without a human's approval.** You may read,
search, propose and prepare a change. You do not decide what is true. If you are unsure whether
something qualifies as certain, it does not — say so rather than writing it.

## Reading

The MCP server is read-only by design and is the intended way in:

| question | tool |
|---|---|
| what does this bank hold? | `list_facts(client)` — the whole bank in one call, loads no models |
| one fact with its provenance | `get_fact(qname)`; `at=` what the bank knew that day, `world_at=` what held in the world |
| you don't know the qname | `search(query, client)` — semantic, over facts and source documents |
| the source says something, but no fact exists yet | `provgraf search-docs "…"` over raw chunks |
| has a dilemma like this been resolved before? | `precedents(query)` — before resolving a new one |
| is the bank healthy? | `check(client?)` — before publishing anything built on these facts |

**Chunks are not facts.** `doc_chunk` holds raw document text for finding things; it carries no
approval and no staleness. Quote facts, not chunks. Promote a chunk to a fact only when a human
approves it.

## Writing

Writes go through the CLI, where validation lives — never through raw SQL, which bypasses the
invariants, and never through the MCP surface, which has no write tools.

```bash
provgraf add-doc <doc> --by <agent> --owner <slug> --file <path>            # file-backed source
provgraf add-doc <doc> --by <agent> --owner <slug> --testimony --date …    # someone vouched for it
provgraf add <qname> --value X --from <doc> --owner <slug> [--status to_confirm]
provgraf revise <qname> --value X --from <new-doc> --by <agent> --note "why"
```

Two things the machinery will do to you, on purpose:

- **A revision made by an agent of `kind='software'` lands as `to_confirm`**, not `confirmed`,
  until a human runs `verify`. Do not pass `--status confirmed` to route around this.
- **A source must be one of two shapes**: a document with a file that still exists, or a
  *testimony* with a named agent and a date. A testimony without who or when is hearsay and
  `check` will report it.

Contradictions are not resolved by overwriting. Record both with `link a alternateOf b`; a human
closes it with `resolve … --basis "…"`, and the rejected alternative stays in the graph.

## If you change the engine

- The staleness hash exists in Python and in SQL. Change one, change the other — a test pins their
  parity, because divergence makes the whole feature lie quietly.
- `check` is computed once in `report.py::gather()` and merely rendered by the CLI and the MCP
  server. Change the report there, never in a renderer.
- Schema changes are new numbered files in `infra/postgres/init/`; deployed ones are immutable.
- Scripts that write must stay re-runnable: `add`, `add-doc` and `revise` take `--skip-existing`.
  Idempotence belongs per entity, not per block of a script — a guard around a whole block
  silently swallows anything added to that block later.

## Tests

`uv run pytest` against a throwaway Postgres. Never point `DATABASE_URL` at a database holding
real data while testing; the demo reset refuses to run against non-demo owners, but do not rely
on that as your only safeguard.
