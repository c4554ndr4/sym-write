# A bounded public beta

The trial and Google sign-in flow are implemented. This repository does not provision a domain, identity-service credentials, a Turnstile site, or hosting. Local mode remains the default. Sign-in is visibly unavailable until configured; there is no simulated success path in the application.

## What a click pays for

A continuation is one explicit request, with up to five candidate model calls and one refinement. Anonymous visitors receive **10 continuations in total**. Google sign-in unlocks **10 per UTC day** by default. Starting a request consumes its allowance, even if the browser cancels or a provider fails: work may already have incurred a cost. Validation failures and rejected admissions do not consume it.

Counts are held on the server in a transactional ledger. They survive restarts and browser storage changes. The same anonymous network shares a trial allowance; IPv6 addresses share it within a /64. Network identifiers are keyed hashes, not raw addresses. A shared school, office, or household may encounter another person's limit. This is a conservative beta policy, not proof that each visitor is a unique person.

Default additional limits are 3 starts per identity per minute, 6 per network per minute, 30 per network per UTC day, and **100 across the whole app per UTC day**. One request can run per identity and two across the app. Reservations are atomic across processes sharing the database. A duplicate request ID is rejected for seven days without more model work. A crashed worker's reservation expires after two minutes; the attempt stays charged. Daily allowances reset at midnight UTC; anonymous trial counters do not reset.

Changing both network and browser identity can obtain another anonymous allowance; creating more Google accounts can obtain more account allowances. Turnstile and network limits make casual automation harder. The shared ceiling and provider key cap bound the service's exposure even when individual identity checks are evaded. They do not establish an abuse-proof system.

## Before serving a public URL

Use one application host with a persistent local volume, behind an HTTPS reverse proxy. SQLite must not be on a shared network filesystem. Multiple independent containers with separate databases would each grant their own allowance: that topology is not supported. Do not expose or tunnel local mode as a public demo.

Set these values in the host's secret store or an ignored `.env` file:

| Setting | Purpose |
| --- | --- |
| `SYMWRITE_MODE=public` | Enables mandatory public-service checks. |
| `SYMWRITE_ORIGIN` | Exact HTTPS origin, such as `https://write.example.com`; no path. |
| `SYMWRITE_SESSION_SECRET` | At least 32 cryptographically random characters. Keep stable across restarts. |
| `SYMWRITE_ACCESS_DB` | Persistent path, for example `/data/symwrite/access.sqlite3`. Back it up with the session secret. |
| `SYMWRITE_GOOGLE_CLIENT_ID` and `SYMWRITE_GOOGLE_CLIENT_SECRET` | Google OAuth web application credentials. Register `<origin>/auth/google/callback` as the exact redirect URI. |
| `SYMWRITE_TURNSTILE_SITE_KEY` and `SYMWRITE_TURNSTILE_SECRET` | A Cloudflare Turnstile **Managed** widget restricted to the public hostname. Only the site key is public. |
| `SYMWRITE_OPENROUTER_API_KEY` | A separate inference key for this app, with an explicit credit limit in OpenRouter. Public mode never falls back to `OPENROUTER_API_KEY`. |
| `SYMWRITE_MAX_KEY_LIMIT_USD` | Largest accepted provider key cap; defaults to $10. This validates an existing provider cap; it does not create one. |
| `SYMWRITE_TRUSTED_PROXIES` | Only the exact immediate proxy addresses/CIDRs authorized to supply `X-Forwarded-For`. Empty means ignore forwarded headers. |

The spending-cap check runs at startup and before each generation. It rejects an uncapped, exhausted, over-budget, or management key and pauses on verification failure. The cap's reset period belongs to the OpenRouter key: a $10 lifetime limit and a $10 daily limit have very different exposure. For an initial demo, use a small **non-resetting** app key limit. This implementation does not change the owner's provider settings. Both public model stages must use OpenRouter, so the same capped key covers every model call. Provider accounting and any in-flight settlement remain subject to provider behavior.

Only the exact author-approved Avalon demo profile is accepted in public mode. Its content digest is pinned in the server. Any other private profile, a modified profile mislabeled as synthetic, or a different model configuration will fail startup. The author-provided Avalon sample is explicitly marked non-synthetic; the exact content digest, rather than that label, determines which profile is allowed. To change the public corpus, review the whole replacement for publication before deliberately updating that digest. Personal profiles remain available in local mode.

Start behind your proxy with a command appropriate to its network; for example, on the same host:

```bash
.venv/bin/uvicorn backend.main:app --app-dir symwrite-app \
  --host 127.0.0.1 --port 8001 --no-access-log --no-proxy-headers
```

`--no-proxy-headers` preserves the actual socket peer so the app can decide which forwarded headers to trust. Configure the proxy to preserve the public Host, overwrite/append the real client address correctly, and accept traffic only through the intended ingress. Do not trust all IPs. With a proxy but no trusted-proxy setting, visitors conservatively share the proxy's allowance. Add edge limits for request volume and body size: app quotas govern model spending, not bandwidth or every kind of denial of service.

Complete Google consent-screen configuration and any required verification, and provide the deployment's actual privacy/contact information. Test the real callback and a real Turnstile challenge before inviting visitors. The automated suite covers controlled identity/challenge responses and signed-token validation; it cannot verify an unconfigured external account.

## Data and credentials

The browser receives an opaque HttpOnly session cookie and a session-bound CSRF token. In public mode the cookie is Secure, SameSite=Lax and host-only. OAuth uses an expiring, single-use state bound to that session, PKCE and a nonce. Google's library verifies token signatures, audience, issuer and expiry; only verified-email accounts are accepted. Sign-in rotates the session. The server retains a keyed account identifier, not the person's name, email, Google access token, or refresh token.

Turnstile tokens are verified by the server on every public writing request; success, hostname and action must match. Missing, invalid, expired, or replayed challenges do not authorize model calls. Provider and OAuth secrets are never placed in frontend code, status responses, model prompts, or the writing profile. Error handling avoids logging provider bodies, authorization codes, tokens and drafts. Configure the reverse proxy similarly: a callback query string contains a temporary authorization code.

The access database stores sessions, hashed identities, counters, OAuth handshake state and request IDs—not document text or suggestions. Expired sessions/handshakes and old short-window counters are pruned during session creation; request IDs are retained seven days. Lifetime trial counters persist to prevent resets. Deleting the database or changing the hashing secret resets abuse history, so neither is a routine deployment step.

Drafts stay in browser storage under the notebook profile. They do **not** become account-synchronized documents after sign-in, and sign-out does not erase them. Other people using the same browser profile can read those drafts. Export important writing and clear the site's browser storage when finished on a shared computer. Generation still sends the selected draft context to the model provider; Google and Cloudflare also receive the data needed for their respective services.

`.env`, `.env.*` except the template, private profiles, `.local`, databases, and common private-key files are ignored. Only the frontend directory is served as static content. Ignoring a file does not remove historical commits. Before making an existing repository public, review its full history for both credentials and private writing. The historical SymWrite repository is still private.

## Verification and references

`pytest` covers the tenth/eleventh request boundary, browser-reset and restart persistence, concurrent last-credit races, replay, account/day/network/global limits, forged forwarded headers, expired sessions, CSRF, OAuth session rotation, invalid identity/challenge responses, budget enforcement, and key routing. `node tests/access-browser.cjs` checks the trial gate, retained drafts, daily limit and sign-out on wide and narrow layouts using controlled responses. It makes no model or real Google calls.

- [Google's OpenID Connect flow](https://developers.google.com/identity/openid-connect/openid-connect)
- [Google's token-verification library](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html)
- [Turnstile server-side validation](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/)
- [OpenRouter key and limit metadata](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)
