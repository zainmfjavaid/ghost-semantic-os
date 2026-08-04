# OSWorld patch set

The runtime uses the OSWorld source tree from commit
`fad6d07f0a3ad456e7d966dcc98a7fee2491afe0` plus the six ordered patches in
this directory. Because upstream no longer advertises that historical commit,
the Apache-licensed base tree is included as a checksummed source archive.
`scripts/bootstrap-osworld.sh` verifies the base tree, applies the patches, and
verifies the resulting Git tree is exactly
`76331b18181423ee60ec613c1475b2d8300b7b03`.

The changes add a screenshot opt-out, application inventory, setup fail-closed
behavior, and three evaluator compatibility fixes. Evaluators remain outside
the model policy loop. See `docs/architecture.md` and `docs/benchmarking.md`.
