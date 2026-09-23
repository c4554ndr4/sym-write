"""Writing service with durable trial admission and optional Google sign-in."""

import asyncio
import hashlib
import ipaddress
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from .access import AccessSettings, AccessStore
from .auth import google_identity, google_url, verify_challenge, verify_spending_cap
from .configuration import APP_DIR, ROOT, load_profile
from .providers import GenerationError, ProviderClient, configured
from .retrieval import Retriever
from .writing import CompletionRequest, complete

load_dotenv(ROOT / ".env")
logger = logging.getLogger("symwrite")
# Author-approved Avalon sample, pinned to prevent other personal writing being served.
PUBLIC_PROFILE_DIGEST = (
    "b69743a4ea00d147898e220a80c6427cf88e392ba122bb04f9b76c2d40a46cb1"
)


class RequestBoundary:
    """Reject oversized streamed bodies and nonlocal sockets before parsing input."""

    def __init__(self, app, public=False):
        self.app, self.public = app, public

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not self.public:
            host = (scope.get("client") or ("unknown", 0))[0]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = (
                    host == "testclient"
                )  # Starlette's in-process testing transport only.
            if not local:
                return await JSONResponse(
                    {"detail": "Local mode accepts loopback connections only."},
                    status_code=403,
                )(scope, receive, send)
        if scope["method"] == "POST":
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 450_000:
                    return await JSONResponse(
                        {"detail": "This document is too large to submit."},
                        status_code=413,
                    )(scope, receive, send)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            return await self.app(scope, bounded_receive, send)
        return await self.app(scope, receive, send)


def create_app(
    profile=None, retriever=None, client=None, settings=None, access_store=None
):
    settings = settings or AccessSettings.from_env()
    settings.validate()

    @asynccontextmanager
    async def lifespan(app):
        app.state.profile = profile or load_profile(
            Path(os.getenv("SYMWRITE_CONFIG", APP_DIR / "config/config.yaml"))
        )
        if settings.public:
            if (
                hashlib.sha256(app.state.profile.model_dump_json().encode()).hexdigest()
                != PUBLIC_PROFILE_DIGEST
            ):
                raise RuntimeError(
                    "Public mode only serves the bundled, author-approved demo profile."
                )
            if any(
                s.provider != "openrouter"
                for s in (
                    app.state.profile.models.candidate,
                    app.state.profile.models.synthesis,
                )
            ):
                raise RuntimeError(
                    "Public mode requires OpenRouter for both stages so one capped key covers all model calls."
                )
            if not configured(app.state.profile.models.candidate, public=True):
                raise RuntimeError(
                    "Public mode requires a separate SYMWRITE_OPENROUTER_API_KEY with a spending cap."
                )
        app.state.access = access_store or AccessStore(settings)
        app.state.profile_version = hashlib.sha256(
            app.state.profile.model_dump_json().encode()
        ).hexdigest()
        app.state.retriever = retriever or Retriever(
            app.state.profile,
            ROOT / ".local/index",
            os.getenv("SYMWRITE_RETRIEVAL", "semantic"),
        )
        app.state.retrieval_error = False
        try:
            await asyncio.to_thread(app.state.retriever.warmup)
        except Exception:
            logger.error(
                "Local retrieval could not initialize; check the model cache or use lexical mode."
            )
            app.state.retrieval_error = True
        async with httpx.AsyncClient() as http:
            app.state.http = http
            app.state.client = client or ProviderClient(http, public=settings.public)
            if settings.public:
                try:
                    await verify_spending_cap(
                        http, settings, app.state.profile.models.candidate
                    )
                except HTTPException:
                    raise RuntimeError(
                        "Public mode requires an available provider key with a verified spending cap."
                    ) from None
            yield

    app = FastAPI(title="SymWrite", lifespan=lifespan, docs_url=None, redoc_url=None)
    allowed_hosts = (
        [urlsplit(settings.origin).hostname]
        if settings.public
        else ["127.0.0.1", "localhost", "[::1]", "testserver"]
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(RequestBoundary, public=settings.public)

    @app.middleware("http")
    async def secure_response(request: Request, call_next):
        origin = request.headers.get("origin")
        expected_origin = (
            settings.origin if settings.public else str(request.base_url).rstrip("/")
        )
        if origin and origin != expected_origin:
            response = JSONResponse(
                {"detail": "Cross-origin requests are not allowed."}, status_code=403
            )
        elif request.headers.get(
            "sec-fetch-site"
        ) == "cross-site" and request.url.path.startswith("/api/"):
            response = JSONResponse(
                {"detail": "Cross-site requests are not allowed."}, status_code=403
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        challenge = " https://challenges.cloudflare.com" if settings.public else ""
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self'{challenge}; style-src 'self'; img-src 'self' data:; connect-src 'self'{challenge}; frame-src 'self'{challenge}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if settings.public:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default errors can echo input, including entire private drafts.
        return JSONResponse(
            {"detail": "Check the document, cursor and request options."},
            status_code=422,
        )

    def set_cookie(response, token):
        response.set_cookie(
            settings.cookie_name,
            token,
            max_age=365 * 86400,
            secure=settings.public,
            httponly=True,
            samesite="lax",
            path="/",
        )

    @app.get("/api/access")
    async def access(request: Request):
        store = app.state.access
        network = store.network(request)
        token = request.cookies.get(settings.cookie_name, "")
        session = store.session(token)
        new = not session
        if new:
            token = store.new_session(network)
            session = store.session(token)
        data = store.allowance(session, network)
        data.update(
            {
                "csrf_token": store.csrf(token),
                "sign_in_ready": settings.login_ready,
                "turnstile_site_key": settings.turnstile_site_key
                if settings.public
                else None,
                "daily_limit": settings.daily_limit,
            }
        )
        response = JSONResponse(data)
        if new:
            set_cookie(response, token)
        return response

    @app.post("/api/auth/google")
    async def login(request: Request):
        store = app.state.access
        session = store.require(request)
        if not settings.login_ready:
            raise HTTPException(
                503, "Sign-in is not configured on this installation yet."
            )
        state, nonce, verifier = store.begin_login(session, store.network(request))
        return {"url": google_url(settings, state, nonce, verifier)}

    @app.get("/auth/google/callback")
    async def callback(request: Request):
        store = app.state.access
        session = store.session(request.cookies.get(settings.cookie_name, ""))
        try:
            nonce, verifier = store.consume_login(
                request.query_params.get("state"), session
            )
            if request.query_params.get("error") or not settings.login_ready:
                raise ValueError()
            subject = await google_identity(
                app.state.http,
                settings,
                request.query_params.get("code"),
                nonce,
                verifier,
            )
            identity = "google:" + store.digest("account", subject)
            token = store.new_session(
                store.network(request), identity, previous=session
            )
        except Exception:
            # No codes, provider bodies, tokens or identity information in logs or URLs.
            return RedirectResponse("/?signin=failed", status_code=303)
        response = RedirectResponse("/", status_code=303)
        set_cookie(response, token)
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        store = app.state.access
        session = store.require(request)
        # Anonymous network trial history survives logging out and account changes.
        token = store.new_session(store.network(request), previous=session)
        response = JSONResponse({"ok": True})
        set_cookie(response, token)
        return response

    @app.get("/api/status")
    async def status():
        p = app.state.profile
        return {
            "profile": p.name,
            "synthetic": p.synthetic,
            "demo": app.state.profile_version == PUBLIC_PROFILE_DIGEST,
            "style": p.style,
            "source_count": len(p.sources),
            "profile_id": p.id,
            "profile_version": app.state.profile_version,
            "retrieval": app.state.retriever.mode,
            "retrieval_ready": not app.state.retrieval_error,
            "generation_ready": configured(p.models.candidate, settings.public)
            and configured(p.models.synthesis, settings.public),
            "candidate": {
                "provider": p.models.candidate.provider,
                "model": p.models.candidate.model,
                "ready": configured(p.models.candidate, settings.public),
            },
            "synthesis": {
                "provider": p.models.synthesis.provider,
                "model": p.models.synthesis.model,
                "ready": configured(p.models.synthesis, settings.public),
            },
            "candidate_count": p.models.candidate_count,
        }

    @app.get("/api/sources")
    async def sources():
        return {
            "profile_version": app.state.profile_version,
            "sources": [source.model_dump() for source in app.state.profile.sources],
        }

    @app.post("/api/complete")
    async def completion(body: CompletionRequest, request: Request):
        store = app.state.access
        session = store.require(request)
        network = store.network(request)
        if body.profile_version != app.state.profile_version:
            raise HTTPException(
                409,
                "The writing profile changed. Export your draft if needed, then reload before requesting another suggestion.",
            )
        if app.state.retrieval_error:
            raise HTTPException(
                503,
                "Local retrieval is unavailable. Check the model download, or restart with SYMWRITE_RETRIEVAL=lexical.",
            )
        stages = [app.state.profile.models.candidate]
        if body.mode == "refine":
            stages.append(app.state.profile.models.synthesis)
        for stage in stages:
            if not configured(stage, settings.public):
                raise HTTPException(
                    503, "The model provider is not configured on this installation."
                )
        allowance = store.allowance(session, network)
        if allowance["sign_in_required"]:
            raise HTTPException(
                401, "Your 10 trial continuations are used. Sign in to continue."
            )
        # Verify before reserving a paid attempt; failed validation never consumes a trial.
        await verify_challenge(
            app.state.http, settings, request.headers.get("x-turnstile-token", "")
        )
        await verify_spending_cap(
            app.state.http, settings, app.state.profile.models.candidate
        )
        store.reserve(session, network, body.request_id)
        try:
            async with asyncio.timeout(90):
                result = await complete(
                    body, app.state.profile, app.state.retriever, app.state.client
                )
                result["access"] = store.allowance(session, network)
                return result
        except GenerationError as error:
            raise HTTPException(502, str(error)) from None
        except TimeoutError:
            raise HTTPException(
                504,
                "The writing request timed out. Your draft has been kept; please try again.",
            ) from None
        except Exception:
            logger.error("Writing request failed; no document content was logged.")
            raise HTTPException(
                500, "The writing request failed. Your draft has been kept."
            ) from None
        finally:
            # Started attempts count even on cancellation/provider failure: calls may have cost money.
            store.finish(session, body.request_id)

    @app.get("/")
    async def index():
        return FileResponse(APP_DIR / "frontend/index.html")

    app.mount("/static", StaticFiles(directory=APP_DIR / "frontend"), name="static")
    return app


app = create_app()
