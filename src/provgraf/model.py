"""Constants mirroring the ENUMs from 01_schema.sql (validation on the CLI side)."""

PROV_CLASSES = {"source", "decision", "derivation"}
STATUSES = {"confirmed", "disputed", "to_confirm", "resolved"}
SCOPES = {"global", "client", "project"}
LOADS = {"eager", "lazy"}
AUDIENCES = {"internal", "client", "public"}
AGENT_KINDS = {"person", "software", "organization"}
ACTIVITY_KINDS = {
    "ingestion", "extraction", "derivation", "verification", "decision", "approval",
}
REL_PREDICATES = {
    "wasDerivedFrom", "alternateOf", "specializationOf", "hadMember",
}
REL_SUBTYPES = {"Revision", "Quotation", "PrimarySource"}
