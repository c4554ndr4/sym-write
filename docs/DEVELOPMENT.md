# Setup and verification

The runtime is a small local server and a browser editor. There is no frontend build step. Python 3.11 is the tested runtime; the dependency locks were resolved for that version.

## Configuration

Copy `.env.example` to `.env` at the repository root. Supply `OPENROUTER_API_KEY`, `GROQ_API_KEY`, or both, according to your profile's model stages. Never put keys in a writing profile. Restart the server after editing configuration.

The included profile is `symwrite-app/config/config.yaml`. Candidate generation and synthesis have independent provider, model, temperature, and output limits. Model names must exist on the selected provider. The number of parallel candidates can be between one and five. Groq uses ordinary interactive requests, rather than a batch job intended for offline processing.

```bash
python symwrite-app/run.py                 # local semantic retrieval
python symwrite-app/run.py --lexical       # word-overlap retrieval; no model download
python symwrite-app/run.py --port 8002
```

Semantic retrieval uses FastEmbed's local `BAAI/bge-small-en-v1.5` model. Model files and SQLite indexes live under ignored `.local/index/`. The index identity includes the embedding model, chunking version, and source content, so editing a profile cannot silently reuse embeddings of its previous sources. Embedding work runs outside the request event loop. Lexical mode uses word overlap and is labelled as such in the interface; it is not represented as semantic search.

The [FastEmbed retrieval documentation](https://qdrant.github.io/fastembed/qdrant/Retrieval_with_FastEmbed/) explains its separate passage and query encoding interfaces. The model transport uses the providers' chat-completion APIs; see [OpenRouter's request reference](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request).

## Bring your own writing

Profiles are validated, portable YAML files. Choose samples deliberately and add only facts you want the writing system to use. The importer reads `.txt` and `.md`; it does not infer a biography, scrape accounts, or send your archive to a provider during import.

```bash
python symwrite-app/profiles.py create \
  --name "My notebook" \
  --writing /absolute/path/to/an-essay.md \
  --writing /absolute/path/to/notes.txt \
  --fact "A fact I want available to the assistant." \
  --output profiles/my-notebook.yaml

python symwrite-app/profiles.py validate profiles/my-notebook.yaml
```

Set `SYMWRITE_CONFIG=/absolute/path/to/profiles/my-notebook.yaml` in `.env`, then restart. Review the new profile's style guidance and model settings before generation. The importer uses the example's defaults for those settings, not an inferred style model. Keep private profiles in the ignored `profiles/` directory.

The profile's stable `id` owns its browser drafts. Keep it when editing that profile; use a distinct ID for a different writer. A content version separately binds each generation request to the profile visible in the browser. If the server restarts with a changed profile, old tabs must reload before sending a draft to the model.

To recover a September 2025 configuration:

```bash
python symwrite-app/profiles.py migrate /path/to/old-config.yaml \
  --output profiles/recovered.yaml
```

Migration preserves its selected facts and writing samples. It supplies current model and style defaults for review. Creating or migrating a profile never overwrites an existing output file or changes the active profile. A profile can be copied as one YAML file; its index rebuilds locally, so there is no separate database to export, import, or accidentally combine with another writer's configuration.

## Runtime behavior

A request contains the document, cursor position, profile version, and requested mode and length. The server bounds model context to 6,000 characters before the cursor, 1,500 after it, style guidance, and up to four source excerpts totaling 2,800 characters. These are character budgets, not token counts. The full document reaches the local server, while only that bounded context reaches model providers. Each source chunk is at most 750 characters; the model's own tokenizer may impose a tighter effective input limit on an individual embedding.

Candidate requests run concurrently. Successful candidates remain usable when a sibling fails. An all-failed request produces an error, never a fabricated completion. A failed refinement leaves the actual candidates available with a visible warning. Empty, malformed, reasoning-only, and output-limit responses are rejected. Copied openings at an unfinished sentence boundary are removed conservatively; this does not detect every form of repetition. Requested length is guidance to the model, rather than a strict word count. There is one refinement call, rather than a chain of speculative alternatives. Each provider call has a 35-second timeout; a writing request has a 90-second deadline. Two writing requests can run at a time, with one per identity. Durable trial and daily allowances are checked before model work.

Browser cancellation and editing prevent a late result from being displayed or inserted. A provider request already in flight may still complete and incur cost. Cancelling is not a promise of a provider-side refund; started attempts remain counted against the allowance.

The browser treats drafts and model output as text. API calls are restricted to the local origin, and the server binds to loopback. Local mode rejects nonloopback connections. The optional public mode adds Google sign-in, server-held quotas, bot checks and a capped provider key; it does not add cloud document storage, per-user corpora or collaboration. See [public beta setup](PUBLIC_BETA.md). Provider account configuration and data retention policies still apply to the text sent to those services.

The dark editor uses a centered, two-column writing frame with compact document and profile menus. Continuations appear as selectable cards; choosing one expands its full text before acceptance. On narrow screens, the continuations follow the editor. The layout takes inspiration from Inkstream while retaining SymWrite's muted charcoal and sage palette.

The editor starts blank; the Avalon invitation loads only when selected from **Examples**. Sources and profile details stay collapsed until requested. Existing edited drafts are preserved when upgrading from automatically seeded examples.

Drafts save in browser storage after edits. Storage failure is shown rather than reported as successful saving. Another tab changing the same notebook pauses local writes until you export any unsaved work and reload. Export important writing; browser data can be cleared, and moving to another host or port gives a different storage origin.

## Checks

Offline backend tests exercise real prompt construction with deterministic provider transports and encoders. They do not require keys or model downloads.

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The browser regression suites require the local server to be running. They intercept generation responses, so it makes no paid model calls. It covers cursor-safe acceptance, undo, draft recovery, export, stale responses, cancellation, inert pasted markup, visible errors, narrow-screen overflow, startup recovery, and cross-tab conflicts.

```bash
npm ci
npx playwright install chromium
npm run test:browser
node tests/access-browser.cjs
```

Set `CHROME_PATH` to an existing Chrome executable instead of installing Chromium if preferred. `SYMWRITE_URL` can select another local port.

The separate `scripts/capture-examples.cjs` script loads the author-approved Avalon sample and captures the editor on desktop and mobile. Its default path makes no model calls:

```bash
node scripts/capture-examples.cjs
```

For a deliberately authorized live capture, `--live` sends the draft and selected context to the configured provider and makes up to six calls charged to that key. It saves the actual response as `docs/examples/avalon.json`; the trial and rate limits still apply. `--recorded` can then redraw that saved response without another paid request. The script refuses profiles other than the exact reviewed demo. The current README screenshots use the default, input-only capture.

Observed historical example timings are documented with their records; they are not controlled latency benchmarks. Groq's transport and mixed-provider routing are covered by deterministic HTTP tests. Historical live screenshots used OpenRouter only.

## Typography

EB Garamond is bundled locally for the reading surface, with its [SIL Open Font License](../symwrite-app/frontend/fonts/OFL.txt). The browser makes no external font requests. Interface controls use the system sans-serif.
