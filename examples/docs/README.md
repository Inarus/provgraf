# Demo source documents

Stand-ins for the real thing. In an actual deployment these are the authority documents a fact
is allowed to come from — a council resolution, a building permit, a registry extract, an audited
financial statement. Each document node in the graph stores the path to its file, and
`provgraf check` reports DANGLING-DOC when that file disappears (broken provenance), plus
ORPHANED for facts whose only source was that document.
