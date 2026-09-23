# Restoration notes

SymWrite began in September 2025 as an experiment in combining retrieved personal writing, parallel language-model continuations, and a refinement stage. The original design and commits remain in repository history.

The September 2026 restoration retains that structure and repairs paths that prevented it from functioning as described:

- Retrieved passages now enter both generation prompts and remain visible during review.
- Each model stage chooses its own provider. The Groq path uses the same interactive interface as OpenRouter.
- Failed provider calls are separate from prose, and a missing API key is an explicit unavailable state. The silent personal mock responses are gone.
- The writing interface stores drafts, offers explicit acceptance and undo, preserves suffix text at the cursor, and discards responses made stale by further editing or navigation.
- A stable writing-profile identity is separate from the source-index fingerprint. Source edits rebuild memory without hiding drafts, and old tabs cannot submit against a changed profile unnoticed.
- The old onboarding scripts inferred personal facts, copied active configuration and databases independently, and included a broken archive roundtrip. They have been replaced by a smaller importer for explicitly selected writing and facts. A profile is one validated file, and indexes are derived artifacts.
- The initial restoration used a fictional research notebook. The current default sample is the author-selected Avalon invitation, with screenshots of its first draft, revision note and second-draft opening before generation. The original personal profile was recovered locally into an ignored private file, outside the screenshot workflow. Historical commits have not been rewritten; this restoration is not a claim that the repository's history is ready for public release.
- Current dependencies, deterministic backend checks, browser regression checks, live generation records, and actual screenshots replace procedural test scripts and unverified performance claims.

The original implementation is available at commit `d95132a3d9feb3b9b6a954a6b730648d59a506b7`. The restored editor should be assessed as a later iteration, not as unchanged evidence of what the prototype could do in 2025.
