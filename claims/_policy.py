"""Per-project policy for tweet-tracker claim-runtime."""
from claim_runtime import ProjectPolicy


class TweetTrackerPolicy(ProjectPolicy):
    """Policy for tweet-tracker's claim-runtime adoption.

    Two Python daemons (Phase 1: tracker.py, Phase 2: cascade_walker.py)
    that snapshot a single tweet's engagement over time and compute
    cascade metrics. Stdlib only — no extra deps.
    """

    # Hard claims are human-only — agent cannot silently weaken invariants.
    require_human_for_new_claims = True

    # Adoption phase — start at 0 and ratchet up as coverage grows.
    min_claims_covering_file = 0

    # Files the refactor daemon must NEVER rewrite (data semantics + auth).
    never_refactor = [
        "scripts/tracker.py",         # Phase 1 daemon — production
        "scripts/cascade_walker.py",  # Phase 2 daemon — production
    ]
