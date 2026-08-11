"""ONE place that computes the `check` report — the CLI and the MCP server only render it.

Why (2026-08-11): the report existed in two copies (main.py and mcp_server.py) and quietly
drifted — the agent-facing one skipped documents with no file path, which is exactly the case
the orphan guard was built for. Two implementations of one report is worse than missing one
of them: the human and the agent were looking at different states of the same bank.

`gather()` is pure (read-only) and returns the computed sections; rendering (rich colours in
the CLI, plain text over MCP) belongs to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from provgraf import completeness, db
from provgraf import conflicts as cf

REMOTE_URI_PREFIXES = ("http://", "https://", "s3://", "gs://", "sftp://")


@dataclass
class Report:
    hard: list = field(default_factory=list)          # an input changed → recompute
    soft: list = field(default_factory=list)          # depends on an overdue source
    overdue: list = field(default_factory=list)       # source due for re-verification
    disputed: list = field(default_factory=list)      # conflicting sources
    suggestions: dict = field(default_factory=dict)   # qname → (pick, reason) — a HINT only
    unresolved: list = field(default_factory=list)    # derivation over a disputed input
    incomplete: list = field(default_factory=list)    # missing required fields (needs client)
    dangling: list = field(default_factory=list)      # (qname, reason) — document without a file
    unattributed: list = field(default_factory=list)  # (qname, reason) — testimony with no who/when
    orphaned: list = field(default_factory=list)      # fact whose ONLY source went missing

    @property
    def total(self) -> int:
        return (len(self.hard) + len(self.soft) + len(self.overdue) + len(self.disputed)
                + len(self.unresolved) + len(self.incomplete) + len(self.dangling)
                + len(self.unattributed) + len(self.orphaned))


async def gather(pool, client: str | None = None, root: Path | None = None) -> Report:
    """Collects every section of the report. `client` enables completeness and narrows the
    documents; `root` is the directory source-file paths resolve against (None = do not check
    file existence, only a missing path)."""
    stale = await db.staleness_rows(pool)
    r = Report(
        hard=[x for x in stale if x["hard_stale"]],
        soft=[x for x in stale if x["soft_stale"] and x["provenance_class"] != "source"],
        overdue=await db.overdue_rows(pool),
        disputed=await db.disputed_rows(pool),
        unresolved=await db.unresolved_derivations(pool),
        incomplete=await completeness.holes(pool, client) if client else [],
    )
    if r.disputed:
        for g in cf.group_disputed(r.disputed, await db.disputed_facts_sources(pool)):
            if g["suggestion"]:
                for m in (g["canonical"], *g["alternates"]):
                    r.suggestions[m] = g["suggestion"]
    # Two shapes of a certain source → two DIFFERENT integrity conditions (see add-doc's docstring):
    #   file-backed — the bank holds the original/a copy, so check that the file is still there;
    #   testimony   — the message came through a person, so check that WHO and WHEN are recorded.
    # A testimony without a file is NOT a defect; a testimony without who/when is a fatal one.
    for d in await db.documents(pool, client):
        f = d.get("file")
        if d["testimony"]:
            missing = []
            if not d["issuer"]:
                missing.append("no record of WHO vouched (no wasAttributedTo)")
            if not d["date"]:
                missing.append("no record of WHEN (no date)")
            if missing:
                r.unattributed.append((d["qname"], " + ".join(missing)))
        elif not f:
            r.dangling.append((d["qname"], "no file path (if this is a spoken source, use --testimony)"))
        elif f.startswith(REMOTE_URI_PREFIXES):
            continue  # remote URI — provenance lives off-disk, not checked here
        elif root is not None and not (root / f).exists():
            r.dangling.append((d["qname"], f"file does not exist: {f}"))
    # shared-source guard: a fact is orphaned when every one of its sources is dangling
    r.orphaned = await db.orphaned_by_docs(pool, [q for q, _ in r.dangling])
    return r
