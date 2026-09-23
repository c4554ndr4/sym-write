"""Durable admission control. The browser never decides whether a call is funded."""

import hashlib
import hmac
import ipaddress
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from .configuration import ROOT


@dataclass(frozen=True)
class AccessSettings:
    public: bool = False
    origin: str = "http://127.0.0.1:8001"
    database: Path = ROOT / ".local/access.sqlite3"
    secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    turnstile_site_key: str = ""
    turnstile_secret: str = ""
    daily_limit: int = 10
    global_daily_limit: int = 100
    network_daily_limit: int = 30
    per_minute: int = 3
    key_limit_usd: float = 10
    trusted_proxies: tuple = ()

    @property
    def cookie_name(self):
        return "__Host-symwrite" if self.public else "symwrite-local"

    @property
    def login_ready(self):
        return bool(self.google_client_id and self.google_client_secret)

    @classmethod
    def from_env(cls):
        mode = os.getenv("SYMWRITE_MODE", "local")
        if mode not in ("local", "public"):
            raise RuntimeError("SYMWRITE_MODE must be local or public.")
        s = cls(
            public=mode == "public",
            origin=os.getenv("SYMWRITE_ORIGIN", "http://127.0.0.1:8001").rstrip("/"),
            database=Path(
                os.getenv("SYMWRITE_ACCESS_DB", ROOT / ".local/access.sqlite3")
            ),
            secret=os.getenv("SYMWRITE_SESSION_SECRET", ""),
            google_client_id=os.getenv("SYMWRITE_GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("SYMWRITE_GOOGLE_CLIENT_SECRET", ""),
            turnstile_site_key=os.getenv("SYMWRITE_TURNSTILE_SITE_KEY", ""),
            turnstile_secret=os.getenv("SYMWRITE_TURNSTILE_SECRET", ""),
            daily_limit=int(os.getenv("SYMWRITE_DAILY_LIMIT", "10")),
            global_daily_limit=int(os.getenv("SYMWRITE_GLOBAL_DAILY_LIMIT", "100")),
            network_daily_limit=int(os.getenv("SYMWRITE_NETWORK_DAILY_LIMIT", "30")),
            per_minute=int(os.getenv("SYMWRITE_PER_MINUTE", "3")),
            key_limit_usd=float(os.getenv("SYMWRITE_MAX_KEY_LIMIT_USD", "10")),
            trusted_proxies=tuple(
                ipaddress.ip_network(v.strip())
                for v in os.getenv("SYMWRITE_TRUSTED_PROXIES", "").split(",")
                if v.strip()
            ),
        )
        s.validate()
        return s

    def validate(self):
        import math

        url = urlsplit(self.origin)
        if not url.hostname or url.path or url.query or url.fragment or url.username:
            raise RuntimeError("SYMWRITE_ORIGIN must be an origin without a path.")
        if (
            min(
                self.daily_limit,
                self.global_daily_limit,
                self.network_daily_limit,
                self.per_minute,
            )
            < 1
        ):
            raise RuntimeError("Usage limits must be positive.")
        if not math.isfinite(self.key_limit_usd) or self.key_limit_usd <= 0:
            raise RuntimeError("A finite positive provider spending cap is required.")
        if self.public:
            if url.scheme != "https" or len(self.secret) < 32:
                raise RuntimeError(
                    "Public mode requires HTTPS and a random session secret of at least 32 characters."
                )
            if not self.login_ready or not (
                self.turnstile_site_key and self.turnstile_secret
            ):
                raise RuntimeError(
                    "Public mode requires Google sign-in and Turnstile credentials."
                )
        elif url.scheme != "http" or url.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise RuntimeError("Local mode only supports a loopback HTTP origin.")


@dataclass(frozen=True)
class Session:
    token_hash: str
    identity: str
    account: bool


class AccessStore:
    def __init__(self, settings, clock=time.time):
        self.settings, self.clock = settings, clock
        settings.validate()
        settings.database.parent.mkdir(parents=True, exist_ok=True)
        secret_file = settings.database.parent / "session-secret"
        secret = settings.secret
        if not secret:
            try:
                fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as f:
                    f.write(secrets.token_urlsafe(48))
            secret = secret_file.read_text().strip()
            if len(secret) < 32:
                raise RuntimeError("The local session secret is invalid.")
        self.secret = secret.encode()
        # Create with restricted permissions before SQLite opens it; WAL lives beside it.
        fd = os.open(settings.database, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(settings.database, 0o600)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, identity TEXT NOT NULL,
                    account INTEGER NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS counters (
                    scope TEXT NOT NULL, bucket INTEGER NOT NULL, used INTEGER NOT NULL,
                    PRIMARY KEY(scope,bucket));
                CREATE TABLE IF NOT EXISTS requests (
                    identity TEXT NOT NULL, request_id TEXT NOT NULL,
                    created REAL NOT NULL, lease REAL NOT NULL,
                    PRIMARY KEY(identity,request_id));
                CREATE INDEX IF NOT EXISTS active_leases ON requests(lease);
                CREATE TABLE IF NOT EXISTS oauth (
                    state TEXT PRIMARY KEY, session TEXT NOT NULL,
                    nonce TEXT NOT NULL, verifier TEXT NOT NULL, expires REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.settings.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def digest(self, purpose, value):
        return hmac.new(
            self.secret, (purpose + ":" + value).encode(), hashlib.sha256
        ).hexdigest()

    def network(self, request):
        # Ignore forwarded headers unless the socket peer is an explicitly trusted proxy.
        peer = request.client.host if request.client else "unknown"
        try:
            address = ipaddress.ip_address(peer)
            if any(address in subnet for subnet in self.settings.trusted_proxies):
                chain = request.headers.get("x-forwarded-for", "").split(",")
                if len(chain) > 10 or not chain[0].strip():
                    raise ValueError()
                for item in reversed(chain):
                    address = ipaddress.ip_address(item.strip())
                    if not any(
                        address in subnet for subnet in self.settings.trusted_proxies
                    ):
                        break
            # Group IPv6 privacy addresses so changing an interface suffix is not a new trial.
            subnet = ipaddress.ip_network(
                f"{address}/{64 if address.version == 6 else 32}", strict=False
            )
            value = str(subnet)
        except ValueError:
            if self.settings.public:
                raise HTTPException(
                    400, "Client network could not be verified."
                ) from None
            value = peer
        return self.digest("network", value)

    def session(self, token):
        if not token or len(token) > 100:
            return None
        hashed = self.digest("session", token)
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE token=? AND expires>?",
                (hashed, self.clock()),
            ).fetchone()
        return Session(hashed, row["identity"], bool(row["account"])) if row else None

    @staticmethod
    def count(db, scope, bucket):
        row = db.execute(
            "SELECT used FROM counters WHERE scope=? AND bucket=?", (scope, bucket)
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def increment(db, scope, bucket):
        db.execute(
            "INSERT INTO counters VALUES (?,?,1) ON CONFLICT(scope,bucket) DO UPDATE SET used=used+1",
            (scope, bucket),
        )

    def new_session(self, network, identity=None, previous=None):
        now = self.clock()
        token = secrets.token_urlsafe(32)
        hashed = self.digest("session", token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Bootstrap limits also bound anonymous session-table growth.
            if not identity:
                bucket = int(now // 3600)
                if self.count(db, "bootstrap:" + network, bucket) >= 20:
                    raise HTTPException(
                        429,
                        "Too many new sessions on this network. Try again in an hour.",
                    )
                self.increment(db, "bootstrap:" + network, bucket)
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            db.execute("DELETE FROM oauth WHERE expires<=?", (now,))
            db.execute(
                "DELETE FROM requests WHERE created<? AND lease<?",
                (now - 7 * 86400, now),
            )
            # Lifetime trial counters intentionally have bucket 0 and never reset.
            db.execute(
                "DELETE FROM counters WHERE bucket>0 AND ((scope LIKE 'minute:%' AND bucket<?) OR (scope LIKE 'bootstrap:%' AND bucket<?) OR (scope LIKE 'login:%' AND bucket<?) OR (scope LIKE 'day:%' AND bucket<?))",
                (
                    int(now // 60) - 1440,
                    int(now // 3600) - 48,
                    int(now // 3600) - 48,
                    int(now // 86400) - 7,
                ),
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (
                    hashed,
                    identity or "anon:" + secrets.token_hex(24),
                    int(identity is not None),
                    now + (30 if identity else 365) * 86400,
                ),
            )
            if previous:
                db.execute("DELETE FROM sessions WHERE token=?", (previous.token_hash,))
        return token

    def csrf(self, token):
        return self.digest("csrf", token)

    def require(self, request):
        token = request.cookies.get(self.settings.cookie_name, "")
        session = self.session(token)
        supplied = request.headers.get("x-csrf-token", "")
        if not session or not hmac.compare_digest(supplied, self.csrf(token)):
            raise HTTPException(
                403,
                "Your session expired. Reload before continuing; your draft is saved in this browser.",
            )
        return session

    def allowance(self, session, network):
        now = self.clock()
        with self.connect() as db:
            if session.account:
                remaining = max(
                    0,
                    self.settings.daily_limit
                    - self.count(db, "day:user:" + session.identity, int(now // 86400)),
                )
                limit = self.settings.daily_limit
            else:
                remaining = max(
                    0,
                    10
                    - max(
                        self.count(db, "trial:" + session.identity, 0),
                        self.count(db, "trial:network:" + network, 0),
                    ),
                )
                limit = 10
            network_remaining = max(
                0,
                self.settings.network_daily_limit
                - self.count(db, "day:network:" + network, int(now // 86400)),
            )
            global_remaining = max(
                0,
                self.settings.global_daily_limit
                - self.count(db, "day:global", int(now // 86400)),
            )
        return {
            "signed_in": session.account,
            "remaining": remaining,
            "limit": limit,
            "available": min(remaining, network_remaining, global_remaining),
            "reset_at": (int(now // 86400) + 1) * 86400,
            "sign_in_required": not session.account and remaining == 0,
            "capacity_available": bool(network_remaining and global_remaining),
        }

    def reserve(self, session, network, request_id):
        now = self.clock()
        day, minute = int(now // 86400), int(now // 60)
        rules = [
            ("day:global", day, self.settings.global_daily_limit),
            ("day:network:" + network, day, self.settings.network_daily_limit),
            ("minute:user:" + session.identity, minute, self.settings.per_minute),
            ("minute:network:" + network, minute, self.settings.per_minute * 2),
        ]
        if session.account:
            rules.append(
                ("day:user:" + session.identity, day, self.settings.daily_limit)
            )
        else:
            rules += [
                ("trial:" + session.identity, 0, 10),
                ("trial:network:" + network, 0, 10),
            ]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM requests WHERE identity=? AND request_id=?",
                (session.identity, request_id),
            ).fetchone():
                raise HTTPException(
                    409,
                    "This request was already submitted. It will not be charged again.",
                )
            # Trial exhaustion has priority over transient caps: always offer signup at ten.
            if not session.account and any(
                self.count(db, scope, bucket) >= limit
                for scope, bucket, limit in rules[-2:]
            ):
                raise HTTPException(
                    401, "Your 10 trial continuations are used. Sign in to continue."
                )
            for scope, bucket, limit in rules:
                if self.count(db, scope, bucket) >= limit:
                    if scope.startswith("minute:"):
                        raise HTTPException(
                            429,
                            "Please wait a minute before requesting another continuation.",
                            headers={"Retry-After": "60"},
                        )
                    raise HTTPException(
                        429,
                        "Today's writing allowance is used. It resets at midnight UTC.",
                        headers={"Retry-After": str(int((day + 1) * 86400 - now) + 1)},
                    )
            if (
                db.execute(
                    "SELECT COUNT(*) FROM requests WHERE lease>?", (now,)
                ).fetchone()[0]
                >= 2
            ):
                raise HTTPException(
                    429,
                    "The writing service is busy. Try again shortly.",
                    headers={"Retry-After": "10"},
                )
            if db.execute(
                "SELECT 1 FROM requests WHERE identity=? AND lease>?",
                (session.identity, now),
            ).fetchone():
                raise HTTPException(
                    429,
                    "Your previous continuation is still running. Please wait.",
                    headers={"Retry-After": "10"},
                )
            for scope, bucket, _ in rules:
                self.increment(db, scope, bucket)
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?)",
                (session.identity, request_id, now, now + 120),
            )

    def finish(self, session, request_id):
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET lease=0 WHERE identity=? AND request_id=?",
                (session.identity, request_id),
            )

    def begin_login(self, session, network):
        now = self.clock()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            bucket = int(now // 3600)
            if self.count(db, "login:" + network, bucket) >= 20:
                raise HTTPException(
                    429, "Too many sign-in attempts. Try again in an hour."
                )
            self.increment(db, "login:" + network, bucket)
            db.execute(
                "DELETE FROM oauth WHERE session=? OR expires<=?",
                (session.token_hash, now),
            )
            db.execute(
                "INSERT INTO oauth VALUES (?,?,?,?,?)",
                (
                    self.digest("oauth", state),
                    session.token_hash,
                    nonce,
                    verifier,
                    now + 600,
                ),
            )
        return state, nonce, verifier

    def consume_login(self, state, session):
        if not state or len(state) > 100 or not session:
            raise ValueError("Invalid sign-in state")
        hashed = self.digest("oauth", state)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM oauth WHERE state=? AND session=? AND expires>?",
                (hashed, session.token_hash, self.clock()),
            ).fetchone()
            if not row:
                raise ValueError("Expired sign-in state")
            db.execute("DELETE FROM oauth WHERE state=?", (hashed,))
            return row["nonce"], row["verifier"]
