"""Hard claim: no hardcoded RapidAPI keys in tracker code.

# Why this claim is hard

tracker.py and cascade_walker.py read Twitter241 API keys from env
vars (TWITTER241_RAPIDAPI_KEY + TWITTER241_RAPIDAPI_KEY_FALLBACK).
A regression that hardcodes a key would:

  1. Make the production primary→fallback rotation impossible —
     env-driven rotation in §12 of orion/docs/server.md depends on
     the script reading env at startup.
  2. Leak the key into git history if committed.
  3. Force a full code redeploy (scp + systemctl restart) every
     time the key rotates, breaking the "edit env, restart, done"
     promise the rest of the platform got in 2026-04-09.

# How it verifies

Forbids the substring `msh` (the RapidAPI key prefix marker) from
appearing as a string literal anywhere in scripts/. Real keys all
have this `msh` byte sequence in them; the env var name itself
does not contain `msh`, so the rule is precise.
"""
from claim_runtime import forbid


NoHardcodedRapidApiKey = forbid(
    id="tweet_tracker.hard.no_hardcoded_rapidapi_key",
    language="python",
    pattern='"*msh*"',
    scope=["scripts"],
    reason=(
        "RapidAPI key literal in source — must read from env var "
        "TWITTER241_RAPIDAPI_KEY[_FALLBACK] instead. See claim docstring."
    ),
)
