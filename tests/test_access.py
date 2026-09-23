"""No external auth, CAPTCHA or model calls: exercise paid-work admission directly."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.access import AccessSettings, AccessStore
from backend.auth import google_identity, verify_challenge, verify_spending_cap
from backend.configuration import APP_DIR, load_profile
from backend.main import create_app
from backend.providers import ProviderClient
from test_writing import LocalContext, RecordingClient, request


@pytest.fixture
def store(tmp_path):
    now = [1_800_000_000.0]
    s = AccessStore(
        AccessSettings(database=tmp_path / "access.db", per_minute=100),
        clock=lambda: now[0],
    )
    s.now = now
    return s


def session(store, network="network", identity=None):
    return store.session(store.new_session(network, identity))


def spend(store, user, count, network="network", prefix="request"):
    for i in range(count):
        store.reserve(user, network, f"{prefix}-{i}")
        store.finish(user, f"{prefix}-{i}")


def denied(code, fn, *args):
    with pytest.raises(HTTPException) as e:
        fn(*args)
    assert e.value.status_code == code


def test_ten_trials_survive_cookie_clearing_and_restart(store):
    user = session(store)
    spend(store, user, 10)
    denied(401, store.reserve, user, "network", "eleventh")
    new_user = session(store)
    denied(401, store.reserve, new_user, "network", "cleared-cookie")
    reopened = AccessStore(store.settings, clock=store.clock)
    denied(401, reopened.reserve, new_user, "network", "restarted")
    assert reopened.allowance(new_user, "network")["sign_in_required"]
    # An exhausted browser also stays exhausted if its network changes.
    denied(401, reopened.reserve, user, "other-network", "moved")


def test_atomic_last_credit_across_two_store_instances(store):
    user = session(store)
    spend(store, user, 9)
    other = AccessStore(store.settings, clock=store.clock)

    def attempt(pair):
        db, name = pair
        try:
            db.reserve(user, "network", name)
            db.finish(user, name)
            return 200
        except HTTPException as e:
            return e.status_code

    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(attempt, [(store, "a"), (other, "b")]))
    assert sorted(statuses) == [200, 401]


def test_replay_never_runs_again_or_spends_twice(store):
    user = session(store)
    spend(store, user, 1)
    denied(409, store.reserve, user, "network", "request-0")
    assert store.allowance(user, "network")["remaining"] == 9


def test_verified_account_has_daily_quota_across_sessions(store):
    user = session(store, identity="google:stable")
    spend(store, user, 10)
    denied(
        429,
        store.reserve,
        session(store, identity="google:stable"),
        "network",
        "new-login",
    )
    store.now[0] += 86400
    store.reserve(user, "network", "tomorrow")
    assert store.allowance(user, "network")["remaining"] == 9


def test_network_and_global_caps_cover_new_accounts_and_networks(store):
    for i in range(3):
        spend(store, session(store, identity=f"google:{i}"), 10, prefix=str(i))
    denied(
        429, store.reserve, session(store, identity="google:four"), "network", "more"
    )
    for i in range(7):
        user = session(store, network=f"network-{i}", identity=f"google:extra-{i}")
        spend(store, user, 10, network=f"network-{i}")
    denied(
        429,
        store.reserve,
        session(store, "another", "google:last"),
        "another",
        "globally-full",
    )


def test_concurrency_lease_and_minute_limits(store):
    user = session(store)
    store.reserve(user, "network", "active")
    denied(429, store.reserve, user, "network", "parallel")
    user2 = session(store, "two")
    store.reserve(user2, "two", "active2")
    denied(429, store.reserve, session(store, "three"), "three", "global-parallel")
    store.now[0] += 121
    store.reserve(user, "network", "after-crash")
    assert store.allowance(user, "network")["remaining"] == 8
    limited = AccessStore(replace(store.settings, per_minute=1), clock=store.clock)
    limited.finish(user, "after-crash")
    denied(429, limited.reserve, user, "network", "rapid")


def test_forged_forwarded_headers_and_ipv6_rotation(store):
    def req(host, forwarded=""):
        return Request(
            {
                "type": "http",
                "client": (host, 123),
                "headers": [(b"x-forwarded-for", forwarded.encode())],
            }
        )

    assert store.network(req("1.2.3.4", "9.9.9.9")) == store.network(req("1.2.3.4"))
    assert store.network(req("2001:db8:1:2::1")) == store.network(
        req("2001:db8:1:2::abcd")
    )
    import ipaddress

    trusted = AccessStore(
        replace(store.settings, trusted_proxies=(ipaddress.ip_network("10.0.0.0/8"),))
    )
    assert trusted.network(req("10.0.0.1", "9.9.9.9, 1.2.3.4")) == trusted.network(
        req("1.2.3.4")
    )


def test_csrf_expiry_and_single_use_oauth_state(store):
    token = store.new_session("network")
    user = store.session(token)

    def req(csrf):
        return Request(
            {
                "type": "http",
                "headers": [
                    (b"cookie", f"symwrite-local={token}".encode()),
                    (b"x-csrf-token", csrf.encode()),
                ],
            }
        )

    denied(403, store.require, req("forged"))
    assert store.require(req(store.csrf(token))) == user
    state, nonce, verifier = store.begin_login(user, "network")
    with pytest.raises(ValueError):
        store.consume_login(state, session(store, "other"))
    assert store.consume_login(state, user) == (nonce, verifier)
    with pytest.raises(ValueError):
        store.consume_login(state, user)
    state, _, _ = store.begin_login(user, "network")
    store.now[0] += 601
    with pytest.raises(ValueError):
        store.consume_login(state, user)
    store.now[0] += 366 * 86400
    denied(403, store.require, req(store.csrf(token)))


def public_settings(store):
    return replace(
        store.settings,
        public=True,
        origin="https://write.example",
        secret="s" * 48,
        google_client_id="client",
        google_client_secret="private-oauth-secret",
        turnstile_site_key="public-sitekey",
        turnstile_secret="private-challenge-secret",
    )


def test_public_fails_closed_without_credentials_or_https(store):
    with pytest.raises(RuntimeError):
        replace(store.settings, public=True).validate()
    s = public_settings(store)
    s.validate()
    for changes in (
        {"origin": "http://write.example"},
        {"secret": "short"},
        {"turnstile_secret": ""},
        {"google_client_secret": ""},
        {"global_daily_limit": 0},
        {"key_limit_usd": float("nan")},
    ):
        with pytest.raises(RuntimeError):
            replace(s, **changes).validate()


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False},
        {"success": True, "hostname": "evil.example", "action": "continue"},
        {"success": True, "hostname": "write.example", "action": "wrong"},
    ],
)
async def test_challenge_checks_success_hostname_and_action(store, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(HTTPException):
            await verify_challenge(http, public_settings(store), "challenge")


async def test_challenge_accepts_valid_result_and_rejects_replay(store):
    calls = []

    def verify(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={
                "success": len(calls) == 1,
                "hostname": "write.example",
                "action": "continue",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(verify)) as http:
        await verify_challenge(http, public_settings(store), "single-use")
        with pytest.raises(HTTPException):
            await verify_challenge(http, public_settings(store), "single-use")


@pytest.mark.parametrize("limit", [None, 0, 100, float("inf"), "10"])
async def test_uncapped_or_excessive_provider_key_is_rejected(store, limit):
    payload = {
        "data": {"limit": limit, "limit_remaining": 5, "is_management_key": False}
    }
    import json

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=json.dumps(payload))
        )
    ) as http:
        with pytest.raises(HTTPException):
            await verify_spending_cap(
                http,
                public_settings(store),
                load_profile(APP_DIR / "config/config.yaml").models.candidate,
            )


async def test_public_provider_uses_only_dedicated_key_and_checks_cap(
    store, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "owner-key-for-tests")
    monkeypatch.setenv("SYMWRITE_OPENROUTER_API_KEY", "beta-key-for-tests")
    calls = []

    def handle(req):
        calls.append(req)
        if req.url.path.endswith("/key"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "limit": 5,
                        "limit_remaining": 5,
                        "is_management_key": False,
                        "limit_reset": "monthly",
                    }
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "New prose."}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        stage = load_profile(APP_DIR / "config/config.yaml").models.candidate
        await verify_spending_cap(http, public_settings(store), stage)
        await ProviderClient(http, public=True).generate(stage, "s", "p")
    assert all(
        req.headers["Authorization"] == "Bearer beta-key-for-tests" for req in calls
    )


async def test_google_checks_nonce_verified_email_and_library_audience(
    store, monkeypatch
):
    claims = {"sub": "123", "nonce": "nonce", "email_verified": True}
    calls = []

    def verify(encoded, audience):
        calls.append((encoded, audience))
        return dict(claims)

    monkeypatch.setattr("backend.auth.verify_google_id_token", verify)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"id_token": "signed-token"})
        )
    ) as http:
        assert (
            await google_identity(
                http, public_settings(store), "code", "nonce", "verifier"
            )
            == "123"
        )
        assert calls == [("signed-token", "client")]
        for update in (
            {"nonce": "wrong"},
            {"email_verified": False},
            {"azp": "other-client"},
        ):
            saved = dict(claims)
            claims.update(update)
            with pytest.raises(ValueError):
                await google_identity(
                    http, public_settings(store), "code", "nonce", "verifier"
                )
            claims.clear()
            claims.update(saved)


def app_client(store, monkeypatch, recorder=None, settings=None):
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key-never-send")
    profile = load_profile(APP_DIR / "config/config.yaml")
    app = create_app(
        profile,
        LocalContext(),
        recorder or RecordingClient(),
        settings=settings or store.settings,
        access_store=store,
    )
    return TestClient(app)


def prepare(http):
    access = http.get("/api/access")
    http.headers["X-CSRF-Token"] = access.json()["csrf_token"]
    body = request().model_dump()
    body["profile_version"] = http.get("/api/status").json()["profile_version"]
    return body, access


def test_http_eleventh_replay_missing_csrf_and_oversized_body_never_call_models(
    store, monkeypatch
):
    recorder = RecordingClient()
    with app_client(store, monkeypatch, recorder) as http:
        body, access = prepare(http)
        assert "HttpOnly" in access.headers["set-cookie"]
        assert "SameSite=lax" in access.headers["set-cookie"]
        assert (
            http.post(
                "/api/complete", json=body, headers={"X-CSRF-Token": "bad"}
            ).status_code
            == 403
        )
        assert not recorder.calls
        for i in range(10):
            body["request_id"] = str(i)
            response = http.post("/api/complete", json=body)
            assert response.status_code == 200, response.text
            assert response.json()["access"]["remaining"] == 9 - i
        assert len(recorder.calls) == 60
        body["request_id"] = "11"
        assert http.post("/api/complete", json=body).status_code == 401
        assert http.post("/api/complete", content=b"x" * 450001).status_code == 413
        assert len(recorder.calls) == 60
        for url in (
            "/.env",
            "/.env.production",
            "/.local/access.sqlite3",
            "/profiles/cassandra.yaml",
            "/static/../.env",
        ):
            assert http.get(url).status_code == 404
        assert "unit-test-key-never-send" not in str(http.get("/api/status").json())
        assert http.post("/api/auth/google").status_code == 503


def test_http_failed_generation_counts_but_invalid_input_does_not(store, monkeypatch):
    with app_client(store, monkeypatch, RecordingClient(failing=range(5))) as http:
        body, _ = prepare(http)
        bad = dict(body, cursor=999999)
        response = http.post("/api/complete", json=bad)
        assert response.status_code == 422 and body["text"] not in response.text
        assert http.get("/api/access").json()["remaining"] == 10
        assert http.post("/api/complete", json=body).status_code == 502
        assert http.get("/api/access").json()["remaining"] == 9
        assert http.post("/api/complete", json=body).status_code == 409


def test_oauth_callback_rotates_session_and_requires_valid_state(store, monkeypatch):
    s = replace(
        store.settings, google_client_id="client", google_client_secret="secret"
    )

    async def identity(*args):
        return "verified-subject"

    monkeypatch.setattr("backend.main.google_identity", identity)
    with app_client(store, monkeypatch, settings=s) as http:
        _, before = prepare(http)
        old_cookie = http.cookies.get(s.cookie_name)
        url = http.post("/api/auth/google").json()["url"]
        params = parse_qs(urlsplit(url).query)
        assert params["code_challenge_method"] == ["S256"]
        assert "secret" not in params
        path = "/auth/google/callback?code=code&state=" + params["state"][0]
        response = http.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert http.cookies.get(s.cookie_name) != old_cookie
        assert store.session(old_cookie) is None
        current = http.get("/api/access").json()
        assert current["signed_in"] is True and current["remaining"] == 10
        assert (
            http.get(path, follow_redirects=False).headers["location"]
            == "/?signin=failed"
        )
        assert http.post("/api/auth/logout").status_code == 403  # Rotated CSRF token.
        http.headers["X-CSRF-Token"] = current["csrf_token"]
        assert http.post("/api/auth/logout").status_code == 200
        assert http.get("/api/access").json()["signed_in"] is False


def test_local_mode_rejects_nonloopback_socket_even_with_local_host(store, monkeypatch):
    http = app_client(store, monkeypatch)
    with TestClient(http.app, client=("203.0.113.5", 123)) as remote:
        assert remote.get("/api/status").status_code == 403


def test_real_google_verifier_rejects_wrong_signature_audience_issuer_and_expiry(
    monkeypatch,
):
    import json
    import time
    from types import SimpleNamespace
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from google.auth import crypt, jwt
    from google.auth.exceptions import GoogleAuthError
    from backend.auth import verify_google_id_token

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    signer = crypt.RSASigner.from_string(pem, key_id="test")

    def certificates(*args, **kwargs):
        assert kwargs["timeout"] == 10
        return SimpleNamespace(status=200, data=json.dumps({"test": public}).encode())

    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: certificates)
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "123",
        "aud": "client",
        "iat": now,
        "exp": now + 60,
    }
    assert verify_google_id_token(jwt.encode(signer, claims), "client")["sub"] == "123"
    for changes in (
        {"aud": "other"},
        {"iss": "https://attacker.example"},
        {"iat": now - 600, "exp": now - 1},
    ):
        with pytest.raises((ValueError, GoogleAuthError)):
            verify_google_id_token(
                jwt.encode(signer, dict(claims, **changes)), "client"
            )
    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong = crypt.RSASigner.from_string(
        other_private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        key_id="test",
    )
    with pytest.raises((ValueError, GoogleAuthError)):
        verify_google_id_token(jwt.encode(wrong, claims), "client")


def test_public_profile_pin_cookie_flags_and_challenge_before_paid_work(
    store, monkeypatch
):
    monkeypatch.setenv("SYMWRITE_OPENROUTER_API_KEY", "test-beta-key")
    s = public_settings(store)
    profile = load_profile(APP_DIR / "config/config.yaml")
    # Even claiming synthetic=True cannot accidentally publish a replacement corpus.
    private = profile.model_copy(
        update={"style": "A private writing profile", "synthetic": True}
    )
    with pytest.raises(RuntimeError, match="author-approved"):
        with TestClient(
            create_app(private, LocalContext(), RecordingClient(), settings=s)
        ):
            pass

    async def capped(*args):
        return None

    monkeypatch.setattr("backend.main.verify_spending_cap", capped)
    public_store = AccessStore(s, clock=store.clock)
    recorder = RecordingClient()
    app = create_app(
        profile, LocalContext(), recorder, settings=s, access_store=public_store
    )
    with TestClient(app, base_url=s.origin, client=("203.0.113.5", 123)) as http:
        body, response = prepare(http)
        cookie = response.headers["set-cookie"]
        assert (
            cookie.startswith("__Host-symwrite=")
            and "Secure" in cookie
            and "HttpOnly" in cookie
            and "Domain=" not in cookie
        )
        assert http.post("/api/complete", json=body).status_code == 403
        assert not recorder.calls
        assert http.get("/api/access").json()["remaining"] == 10
        assert (
            http.post(
                "/api/complete",
                json=body,
                headers={"Origin": "https://attacker.example"},
            ).status_code
            == 403
        )
        assert response.headers["strict-transport-security"] == "max-age=31536000"
