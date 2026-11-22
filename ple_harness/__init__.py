"""ple_harness: evidence-driven harness intelligence for PLE tasks.

Modules (Phase B of the build pass):
- contract: deliverable-contract extraction and deterministic validation
- authority: document authority / revision resolution
- evidence: the evidence ledger (source vs interpretation vs computed)
- context: prompt-prefix stability, tool-output trimming, compaction state

Design rules: no task IDs, no hardcoded answers, no invented schemas; every
engineering statement stays traceable to its source; anything the model
asserts without verification remains 'unverified' until a verifier confirms it.
"""
