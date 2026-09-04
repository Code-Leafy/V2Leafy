

import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import struct
import time
import uuid as _uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Protocol
from urllib.parse import quote, urlparse

try:
    import brotli
except ImportError:  
    import brotlicffi as brotli
import orjson
import psutil
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, Field, computed_field

LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()


class JsonFormatter(logging.Formatter):
    

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        for key in ("client_id", "conn_id", "remote", "destination"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return orjson.dumps(payload).decode()


if LOG_FORMAT == "json":
    _handler = logging.StreamHandler()
    _handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[_handler])
else:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("leafy")


def _log_ctx(client_id: str = "", **extra) -> dict:
    return dict(client_id=client_id, **extra)





APP_TITLE = "V2Leafy"
DEFAULT_PORT = 8080

SESSION_COOKIE = "leafy_session"
SESSION_TTL = 60 * 60 * 24 * 7          
SESSION_CLEANUP_EVERY = 300             

MAX_HTTP_BODY_BYTES = 1 * 1024 * 1024
MAX_WS_FRAME_BYTES = 512 * 1024
MAX_CLIENTS = 1000
MAX_DASHBOARD_CONNECTIONS = 8
MAX_DASHBOARD_QUEUE = 64
MAX_LOG_ENTRIES = 300
MAX_SUB_ENTRIES = 50
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TIMEOUT_SECONDS = 30
TELEMETRY_INTERVAL_SECONDS = 2.5          
CPU_SAMPLE_INTERVAL_SECONDS = 5.0
PERSIST_INTERVAL_SECONDS = 30
RELAY_BUF = 64 * 1024
RELAY_BUF_MIN = 16 * 1024
RELAY_BUF_MAX = 512 * 1024





LOGIN_RATE_LIMIT = int(os.environ.get("LOGIN_RATE_LIMIT", "5"))
LOGIN_RATE_WINDOW_SECONDS = 60
SESSION_ROTATE_SECONDS = 30 * 60

TCP_CONNECT_TIMEOUT = float(os.environ.get("TCP_CONNECT_TIMEOUT", "5"))
TCP_FIRST_BYTE_TIMEOUT = float(os.environ.get("TCP_FIRST_BYTE_TIMEOUT", "10"))
TCP_IDLE_TIMEOUT = float(os.environ.get("TCP_IDLE_TIMEOUT", "300"))
WS_HANDSHAKE_TIMEOUT = 15.0
RELAY_QUEUE_MAX = int(os.environ.get("RELAY_QUEUE_MAX", "8"))       
RELAY_QUEUE_FULL_TIMEOUT = 30.0
TUNNEL_PING_INTERVAL = 25.0

CONN_POOL_TTL = 30.0
CONN_POOL_MAX = 16
UDP_FORWARDING_ENABLED = os.environ.get("UDP_FORWARDING", "0") == "1"
PADDING_MAX = int(os.environ.get("HANDSHAKE_PADDING_MAX", "0"))    

SUB_CACHE_TTL = 30.0

MEMORY_WATCHDOG_PCT = float(os.environ.get("MEMORY_WATCHDOG_PCT", "80"))
MEMORY_WATCHDOG_MB = float(os.environ.get("MEMORY_WATCHDOG_MB", "0"))
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
ALERT_WEBHOOK_TYPE = os.environ.get("ALERT_WEBHOOK_TYPE", "discord")
ALERT_CPU_PCT = float(os.environ.get("ALERT_CPU_PCT", "85"))
ALERT_MEM_PCT = float(os.environ.get("ALERT_MEM_PCT", "90"))
ALERT_COOLDOWN_SECONDS = 600

QUOTA_RESET_CYCLE = os.environ.get("QUOTA_RESET_CYCLE", "none").lower()
QUOTA_RESET_MONTHLY_DAY = int(os.environ.get("QUOTA_RESET_MONTHLY_DAY", "1"))
QUOTA_RESET_HOUR_UTC = int(os.environ.get("QUOTA_RESET_HOUR_UTC", "0"))

GEO_LOOKUP_ENABLED = os.environ.get("GEO_LOOKUP", "1") == "1"
GEO_CACHE_TTL = 24 * 3600

PROMETHEUS_ENABLED = os.environ.get("PROMETHEUS", "1") == "1"

PBKDF2_ITERATIONS = 600_000
STATE_VERSION = 1
STATE_FILE_NAME = "unified_state.json"
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
STATE_FILE_PATH = STORAGE_DIR / STATE_FILE_NAME
INDEX_HTML_PATH = BASE_DIR / "index.html"
STATIC_DIR = BASE_DIR / "static"

_FALLBACK_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>V2Leafy</title></head>
<body><h1>V2Leafy</h1><p>Running. index.html was not found next to main.py.</p></body></html>"""


def get_listen_port() -> int:
    raw = os.environ.get("PORT", str(DEFAULT_PORT))
    try:
        parsed = int(raw)
        return parsed if parsed > 0 else DEFAULT_PORT
    except (ValueError, TypeError):
        return DEFAULT_PORT


_SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not _SECRET_KEY:
    _SECRET_KEY = "leafy-local-development-secret"
    logger.warning(
        "SECRET_KEY is not set; using a local-development key. "
        "Railway generates SECRET_KEY automatically via railway.json."
    )





RAILWAY_MARKERS = (
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_PUBLIC_DOMAIN",
)


class Platform(str, Enum):
    RAILWAY = "railway"
    CODESPACES = "codespaces"
    LOCAL = "local"
    UNKNOWN = "unknown"


def detect_platform() -> Platform:
    
    if (
        os.environ.get("CODESPACES", "").lower() == "true"
        or os.environ.get("CODESPACE_NAME")
        or os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    ):
        return Platform.CODESPACES
    if any(os.environ.get(key) for key in RAILWAY_MARKERS):
        return Platform.RAILWAY
    return Platform.LOCAL


@dataclass(frozen=True)
class ThemeTokens:
    accent: str
    accent_hover: str
    accent_background: str
    success: str
    selection: str


RAILWAY_THEME = ThemeTokens(
    accent="#8b5cf6",
    accent_hover="#7c3aed",
    accent_background="rgba(139, 92, 246, 0.15)",
    success="#8b5cf6",
    selection="rgba(139, 92, 246, 0.35)",
)

CODESPACES_THEME = ThemeTokens(
    accent="#a1a1aa",
    accent_hover="#d4d4d8",
    accent_background="rgba(161, 161, 170, 0.15)",
    success="#a1a1aa",
    selection="rgba(161, 161, 170, 0.35)",
)

LOCAL_THEME = RAILWAY_THEME
UNKNOWN_THEME = RAILWAY_THEME


@dataclass(frozen=True)
class PlatformCapabilities:
    websocket_gateway: bool = True
    subscription_links: bool = True
    relay_tools: bool = False            
    persistent_storage: bool = False
    codespaces_metadata: bool = False


def capabilities_for(platform: Platform) -> PlatformCapabilities:
    if platform is Platform.CODESPACES:
        return PlatformCapabilities(persistent_storage=True, codespaces_metadata=True)
    if platform is Platform.RAILWAY:
        return PlatformCapabilities(persistent_storage=False)
    return PlatformCapabilities(persistent_storage=True)


@dataclass(frozen=True)
class PlatformContext:
    platform: Platform
    display_name: str
    theme: ThemeTokens
    public_base_url: str
    bind_port: int
    is_codespaces: bool
    show_codespaces_info: bool
    persistence_mode: str                 
    capabilities: PlatformCapabilities
    codespaces_name: str = ""
    codespaces_domain: str = ""


def build_platform_context(platform: Optional[Platform] = None) -> PlatformContext:
    p = platform or detect_platform()
    port = get_listen_port()

    if p is Platform.CODESPACES:
        name = os.environ.get("CODESPACE_NAME", "")
        fwd = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
        base = f"https://{name}-{port}.{fwd}" if name else f"http://localhost:{port}"
        return PlatformContext(
            platform=p, display_name="GitHub Codespaces", theme=CODESPACES_THEME,
            public_base_url=base, bind_port=port, is_codespaces=True,
            show_codespaces_info=True, persistence_mode="workspace",
            capabilities=capabilities_for(p), codespaces_name=name, codespaces_domain=fwd,
        )

    if p is Platform.RAILWAY:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip().lower()
        base = f"https://{domain}" if domain else f"http://localhost:{port}"
        return PlatformContext(
            platform=p, display_name="Railway", theme=RAILWAY_THEME,
            public_base_url=base, bind_port=port, is_codespaces=False,
            show_codespaces_info=False, persistence_mode="memory",
            capabilities=capabilities_for(p),
        )

    if p is Platform.LOCAL:
        return PlatformContext(
            platform=p, display_name="Local Development", theme=LOCAL_THEME,
            public_base_url=f"http://localhost:{port}", bind_port=port, is_codespaces=False,
            show_codespaces_info=False, persistence_mode="workspace",
            capabilities=capabilities_for(p),
        )

    return PlatformContext(
        platform=Platform.UNKNOWN, display_name="Unknown", theme=UNKNOWN_THEME,
        public_base_url=f"http://localhost:{port}", bind_port=port, is_codespaces=False,
        show_codespaces_info=False, persistence_mode="memory",
        capabilities=capabilities_for(Platform.UNKNOWN),
    )


PLATFORM_CTX = build_platform_context()







class AuthState(BaseModel):
    password_hash: str = ""
    pass_setup: bool = False


class ClientState(BaseModel):
    id: str
    name: str = "Client"
    limit: float = 0.0
    limit_bytes: int = 0
    used_bytes: int = 0
    upload_bytes: int = 0
    download_bytes: int = 0
    expiry: str = ""
    status: int = 1
    active: bool = True
    utls: str = "chrome"
    created_at: str = ""
    ws_token: str = ""
    sub_slug: str = ""
    billing_cycle: str = "none"
    next_reset_date: str = ""

    @computed_field
    @property
    def usage(self) -> float:
        
        return round(self.used_bytes / (1024.0 ** 3), 3)


class SubEntry(BaseModel):
    id: str = ""
    type: str = "proxy"          
    transport: str = "ws"
    name: str = ""
    ipAddress: str = ""


class AppState(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    version: int = STATE_VERSION
    clients: list[ClientState] = Field(default_factory=list)
    sub_client_subscriptions: dict[str, list[SubEntry]] = Field(
        default_factory=dict, alias="subClientSubscriptions"
    )
    settings: dict = Field(default_factory=dict)
    custom_domain: str = ""
    custom_addresses: list[str] = Field(default_factory=list)
    auth: AuthState = Field(default_factory=AuthState)
    uptime_tracking: dict = Field(default_factory=dict)


class StateStore(Protocol):
    async def load(self) -> Optional[AppState]: ...

    async def save(self, state: AppState) -> None: ...


class MemoryStateStore:
    

    async def load(self) -> Optional[AppState]:
        return None

    async def save(self, state: AppState) -> None:
        return None


class FileStateStore:
    

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> Optional[AppState]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            state = AppState.model_validate(data)
            if state.version != STATE_VERSION:
                logger.warning("State version mismatch (%s); starting fresh", state.version)
                return None
            return state
        except Exception as exc:
            logger.warning("Invalid persisted state; starting fresh: %s", exc)
            return None

    async def save(self, state: AppState) -> None:
        async with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(
                    json.dumps(state.model_dump(mode="json", by_alias=True), indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except Exception as exc:
                logger.warning("Could not persist state: %s", exc)


class AppStateManager:
    

    def __init__(self, store: StateStore):
        self.store = store
        self.state = AppState()
        self.lock = asyncio.Lock()

    async def init(self) -> None:
        loaded = await self.store.load()
        if loaded is not None:
            self.state = loaded
        else:
            self.state = AppState()
        ensure_default_client(self.state)
        await self.store.save(self.state)

    async def persist(self) -> None:
        async with self.lock:
            await self.store.save(self.state)

    def snapshot(self) -> dict:
        return {
            "clients": [c.model_dump(mode="json") for c in self.state.clients],
            "subClientSubscriptions": {
                k: [e.model_dump(mode="json") for e in v]
                for k, v in self.state.sub_client_subscriptions.items()
            },
            "settings": self.state.settings,
        }


def select_store() -> StateStore:
    if PLATFORM_CTX.persistence_mode == "memory":
        return MemoryStateStore()
    return FileStateStore(STATE_FILE_PATH)


STATE_MGR = AppStateManager(select_store())


def _client_ws_token(client: ClientState) -> str:
    return client.ws_token or secrets.token_urlsafe(24)


def backfill_client_secrets(state: AppState) -> None:
    
    for c in state.clients:
        changed = False
        if c.upload_bytes == 0 and c.download_bytes == 0 and c.used_bytes > 0:
            c.upload_bytes = c.used_bytes
            c.used_bytes = c.upload_bytes + c.download_bytes
            changed = True
        if not c.ws_token:
            c.ws_token = secrets.token_urlsafe(24)
            changed = True
        if not c.sub_slug:
            c.sub_slug = "token_" + secrets.token_urlsafe(16)
            changed = True
        if not c.billing_cycle:
            c.billing_cycle = QUOTA_RESET_CYCLE
            if c.billing_cycle in ("monthly", "weekly") and not c.next_reset_date:
                c.next_reset_date = compute_next_reset(c.billing_cycle)
            changed = True


def compute_next_reset(cycle: str, from_ts: Optional[float] = None) -> str:
    base = datetime.fromtimestamp(from_ts, tz=timezone.utc) if from_ts else datetime.now(timezone.utc)
    if cycle == "weekly":
        nxt = base + timedelta(days=7)
        nxt = nxt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif cycle == "monthly":
        nxt = base.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
        nxt = nxt.replace(day=QUOTA_RESET_MONTHLY_DAY, hour=QUOTA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
        if nxt <= base:
            nxt = nxt.replace(day=1) + timedelta(days=32)
            nxt = nxt.replace(day=QUOTA_RESET_MONTHLY_DAY, hour=QUOTA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    else:
        return ""
    return nxt.isoformat()


def ensure_default_client(state: AppState) -> None:
    if state.clients:
        backfill_client_secrets(state)
        return
    cid = generate_uuid()
    state.clients.append(ClientState(id=cid, name="Default", created_at=datetime.now().isoformat()))
    backfill_client_secrets(state)
    state.sub_client_subscriptions[cid] = [
        SubEntry(
            id="info-" + secrets.token_hex(4),
            type="info",
            name="📢 Welcome to V2Leafy | %data-used%GB / %data-total%",
        ),
        SubEntry(
            id="ws-" + secrets.token_hex(4),
            type="proxy",
            transport="ws",
            name="⚡ %client-name%-WebSocket",
        ),
    ]







class SetupRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    password: str = Field(min_length=4, max_length=128, alias="pass")


class LoginRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    password: str = Field(min_length=1, max_length=128, alias="pass")


class ClientCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    limit_value: float = Field(default=0.0, ge=0, le=1_000_000_000)
    limit_unit: Literal["GB", "MB"] = "GB"
    expiry: str = Field(default="", max_length=40)
    active: Optional[bool] = None


class ClientPatchRequest(BaseModel):
    active: Optional[bool] = None
    label: Optional[str] = Field(default=None, min_length=1, max_length=60)
    limit_value: Optional[float] = Field(default=None, ge=0, le=1_000_000_000)
    expiry: Optional[str] = Field(default=None, max_length=40)
    reset_usage: Optional[bool] = None
    billing_cycle: Optional[Literal["none", "monthly", "weekly"]] = None


class ActionRequest(BaseModel):
    action: Literal["start", "stop", "restart", "clear_logs"]


class StateUpdateRequest(BaseModel):
    state: Optional[dict] = None
    reason: str = Field(default="sync", max_length=60)


class ClientStateUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", max_length=64)
    name: str = Field(default="Client", max_length=60)
    limit: float = Field(default=0.0, ge=0, le=1_000_000_000)
    usage: float = Field(default=0.0, ge=0)
    status: int = Field(default=1, ge=0, le=1)
    expiry: str = Field(default="", max_length=40)
    utls: str = Field(default="chrome", max_length=30)
    created_at: str = Field(default="", max_length=40)


class SubEntryUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", max_length=64)
    type: Literal["proxy", "info"] = "proxy"
    name: str = Field(default="", max_length=120)
    ipAddress: str = Field(default="", max_length=200)


class StateUpdateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    clients: Optional[list[ClientStateUpdate]] = None
    sub_client_subscriptions: Optional[dict[str, list[SubEntryUpdate]]] = Field(
        default=None, alias="subClientSubscriptions"
    )
    settings: Optional[dict[str, Any]] = None






SESSIONS: dict[str, dict] = {}     
SESSIONS_LOCK = asyncio.Lock()
LOGIN_ATTEMPTS: dict[str, collections.deque] = {}
LOGIN_LOCK = asyncio.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64] or "unknown"
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


async def check_rate_limit(ip: str) -> bool:
    
    async with LOGIN_LOCK:
        now = time.time()
        dq = LOGIN_ATTEMPTS.setdefault(ip, collections.deque())
        while dq and dq[0] < now - LOGIN_RATE_WINDOW_SECONDS:
            dq.popleft()
        if len(dq) >= LOGIN_RATE_LIMIT:
            return False
        dq.append(now)
        return True


def _origin_check(request: Request) -> bool:
    
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    host = _origin_host(origin)
    if not host:
        return False
    server_host = request.headers.get("host", "").split(":")[0].lower()
    if server_host and host == server_host:
        return True
    return origin_allowed(origin)


def _csrf_token_for(session_token: str) -> str:
    
    return base64.urlsafe_b64encode(
        hashlib.sha256((_SECRET_KEY + ":csrf:" + session_token).encode()).digest()
    ).decode().rstrip("=")


def _csrf_valid(request: Request, session_token: Optional[str]) -> bool:
    if not session_token:
        return False
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied:
        return False
    expected = _csrf_token_for(session_token)
    return secrets.compare_digest(supplied, expected)


def rotate_session_if_due(token: str) -> Optional[str]:
    
    entry = SESSIONS.get(token)
    if not entry:
        return None
    if time.time() - entry["created"] < SESSION_ROTATE_SECONDS:
        return None
    new_token = secrets.token_urlsafe(32)
    SESSIONS.pop(token, None)
    SESSIONS[new_token] = entry
    return new_token


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}"
        f"${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(dk).decode()}"
    )


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


def session_valid_sync(token: Optional[str]) -> bool:
    if not token:
        return False
    entry = SESSIONS.get(token)
    if entry is None or entry["exp"] < time.time():
        SESSIONS.pop(token, None)
        return False
    return True


async def is_valid_session(token: Optional[str]) -> bool:
    async with SESSIONS_LOCK:
        return session_valid_sync(token)


async def create_session() -> str:
    token = secrets.token_urlsafe(32)
    async with SESSIONS_LOCK:
        SESSIONS[token] = {"exp": time.time() + SESSION_TTL, "created": time.time()}
    return token


async def destroy_session(token: Optional[str]) -> None:
    if token:
        async with SESSIONS_LOCK:
            SESSIONS.pop(token, None)


async def require_auth(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not await is_valid_session(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    new_token = rotate_session_if_due(token)
    if new_token:
        request.state.rotated_session = new_token
        return new_token
    return token


async def require_csrf(request: Request, token: str = Depends(require_auth)) -> str:
    
    if not _csrf_valid(request, token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return token


async def session_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(SESSION_CLEANUP_EVERY)
        now = time.time()
        async with SESSIONS_LOCK:
            for token in [k for k, v in SESSIONS.items() if v < now]:
                SESSIONS.pop(token, None)


def _cookie_secure(request: Request) -> bool:
    if PLATFORM_CTX.platform in (Platform.RAILWAY, Platform.CODESPACES):
        return True
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return request.url.scheme == "https"


def _set_session_cookie(request: Request, resp: Response, token: str) -> None:
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )






UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def generate_uuid() -> str:
    return str(_uuid.uuid4())


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(value: str, max_len: int = 120, fallback: str = "") -> str:
    
    if value is None:
        return fallback
    cleaned = _CONTROL_CHARS_RE.sub("", str(value)).strip()
    cleaned = cleaned.replace("<", "").replace(">", "").replace("\"", "")
    return cleaned[:max_len] if cleaned else fallback


def sanitize_client_name(value: str, max_len: int = 60) -> str:
    name = sanitize_text(value, max_len=max_len, fallback="Client")
    if not name:
        name = "Client"
    return name


def require_valid_uuid(value: str) -> str:
    
    if not UUID_RE.match(value or ""):
        raise HTTPException(status_code=400, detail="Invalid identifier format")
    return value.lower()


def public_host(ctx: Optional[PlatformContext] = None) -> str:
    ctx = ctx or PLATFORM_CTX
    return urlparse(ctx.public_base_url).hostname or "localhost"


def use_tls(ctx: Optional[PlatformContext] = None) -> bool:
    ctx = ctx or PLATFORM_CTX
    return urlparse(ctx.public_base_url).scheme == "https"


def generate_vless_link(
    client_id: str,
    remark: str = "V2Leafy Node",
    address: Optional[str] = None,
    ctx: Optional[PlatformContext] = None,
    ws_token: str = "",
) -> str:
    
    ctx = ctx or PLATFORM_CTX
    host = (address or public_host(ctx)).strip()
    if host.startswith("["):
        host = host[1:host.index("]")]
    elif host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        host = host.rsplit(":", 1)[0]

    tls = use_tls(ctx)
    port = 443 if tls else ctx.bind_port
    path = f"/ws/{client_id}?token={ws_token}" if ws_token else f"/ws/{client_id}"

    params = {"encryption": "none"}
    if tls:
        params.update({"security": "tls", "sni": host, "fp": "chrome", "alpn": "http/1.1"})
    else:
        params["security"] = "none"
    params.update({"type": "ws", "host": host, "path": path})

    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"vless://{client_id}@{host}:{port}?{query}#{quote(remark)}"


def resolve_name_placeholders(text: str, client: ClientState) -> str:
    if not text:
        return "V2Leafy Node"
    used_gb = round(client.used_bytes / (1024.0 ** 3), 2)
    limit_gb = client.limit
    limit_str = f"{limit_gb:.2f}GB" if limit_gb > 0 else "Unlimited"
    remain_str = f"{max(0.0, limit_gb - used_gb):.2f}GB" if limit_gb > 0 else "Unlimited"
    exp_str = client.expiry[:10] if client.expiry else "Never"

    t = text
    t = t.replace("%client-name%", client.name)
    t = t.replace("%data-used%", f"{used_gb:.2f}")
    t = t.replace("%data-total%", limit_str)
    t = t.replace("%data-remain%", remain_str)
    t = t.replace("%expiry-date%", exp_str)
    return t


def build_single_sub_entry_link(
    ctx: PlatformContext,
    client: ClientState,
    entry_type: str,
    name: str,
    ip: str = "",
) -> str:
    
    remark = resolve_name_placeholders(name, client)
    if entry_type == "info":
        
        return f"trojan://{generate_uuid()}@127.0.0.1:80?security=none#{quote(remark)}"
    domain = public_host(ctx)
    address = (ip or "").strip() or domain
    return generate_vless_link(client.id, remark=remark, address=address, ctx=ctx, ws_token=client.ws_token)


def build_client_sub_links(
    state: AppState, client: ClientState, ctx: Optional[PlatformContext] = None
) -> list[str]:
    ctx = ctx or PLATFORM_CTX
    entries = state.sub_client_subscriptions.get(client.id, [])
    links: list[str] = [
        build_single_sub_entry_link(ctx, client, e.type, e.name, e.ipAddress)
        for e in entries[:MAX_SUB_ENTRIES]
    ]

    if not links:
        links.append(
            generate_vless_link(
                client.id, remark=f"V2Leafy🍃 {client.name}-Direct", ctx=ctx, ws_token=client.ws_token
            )
        )
        for i, addr in enumerate(state.custom_addresses):
            if addr:
                links.append(
                    generate_vless_link(
                        client.id,
                        remark=f"V2Leafy🍃 {client.name}-Node{i + 1}",
                        address=addr,
                        ctx=ctx,
                        ws_token=client.ws_token,
                    )
                )
    return links





SUB_LINK_CACHE: dict[str, tuple[float, list[str]]] = {}
SUB_CACHE_LOCK = asyncio.Lock()


def invalidate_sub_cache() -> None:
    SUB_LINK_CACHE.clear()


async def cached_client_sub_links(
    state: AppState, client: ClientState, ctx: Optional[PlatformContext] = None
) -> list[str]:
    
    now = time.time()
    cached = SUB_LINK_CACHE.get(client.id)
    if cached and now - cached[0] < SUB_CACHE_TTL:
        return cached[1]
    links = build_client_sub_links(state, client, ctx)
    SUB_LINK_CACHE[client.id] = (now, links)
    return links






stats = {
    "rx_bytes": 0,
    "tx_bytes": 0,
    "total_bytes": 0,
    "total_errors": 0,
    "start_time": time.time(),
}


if PROMETHEUS_ENABLED:
    _P_RX = Counter("v2leafy_traffic_rx_bytes_total", "Bytes downloaded by clients")
    _P_TX = Counter("v2leafy_traffic_tx_bytes_total", "Bytes uploaded by clients")
    _P_CONNS = Gauge("v2leafy_active_connections", "Active proxy connections")
    _P_CLIENT_USED = Gauge(
        "v2leafy_client_used_bytes", "Per-client used bytes", ["client_id", "name"]
    )
    _P_CLIENT_LIMIT = Gauge(
        "v2leafy_client_limit_bytes", "Per-client limit bytes", ["client_id", "name"]
    )
    _P_CPU = Gauge("v2leafy_system_cpu_percent", "CPU usage percent")
    _P_RAM = Gauge("v2leafy_system_ram_mb", "Process RSS in MB")
    _P_RAM_TOTAL = Gauge("v2leafy_system_ram_total_mb", "Available RAM in MB")
    _P_DISK = Gauge("v2leafy_system_disk_percent", "Disk usage percent")

gateway = {
    "status": "running",      
    "started_at": time.time(),
}

_speed = {
    "last_time": time.time(),
    "last_rx": 0,
    "last_tx": 0,
    "down_mbps": 0.0,
    "up_mbps": 0.0,
}

console_logs: collections.deque = collections.deque(maxlen=MAX_LOG_ENTRIES)
proxy_connections: dict[str, dict] = {}


CONNS_HISTORY_WINDOW_MINUTES = 24 * 60
_conns_minute_log: dict = {}


class GatewayStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    RESTARTING = "restarting"


def gateway_running() -> bool:
    return gateway["status"] == GatewayStatus.RUNNING.value


def gateway_uptime_sec() -> int:
    return max(0, int(time.time() - gateway["started_at"]))


_MEM_LIMIT_CACHE: Optional[float] = None


def container_memory_limit_mb() -> float:
    
    global _MEM_LIMIT_CACHE
    if _MEM_LIMIT_CACHE is not None:
        return _MEM_LIMIT_CACHE
    candidates = [
        ("/sys/fs/cgroup/memory.max", "cgroupv2"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "cgroupv1"),
    ]
    for path, _kind in candidates:
        try:
            raw = Path(path).read_text().strip()
            if raw in ("", "max"):
                continue
            limit = int(raw)
            if limit > 0 and limit < (1 << 62):
                _MEM_LIMIT_CACHE = round(limit / (1024 * 1024), 0)
                return _MEM_LIMIT_CACHE
        except (OSError, ValueError):
            continue
    _MEM_LIMIT_CACHE = 0.0
    return _MEM_LIMIT_CACHE


def add_log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    console_logs.append(line)
    logger.info(msg)
    try:
        asyncio.get_running_loop().create_task(_broadcast_log(line))
    except RuntimeError:
        pass


async def _broadcast_log(line: str) -> None:
    await dashboard_mgr.broadcast("log_entry", {"line": line})


def check_client_quota(client: ClientState, extra_bytes: int) -> bool:
    if not client.active or not client.status:
        return False
    limit_b = client.limit_bytes
    if limit_b > 0 and (client.used_bytes + extra_bytes) > limit_b:
        return False
    return True


def record_traffic(client: ClientState, size: int, from_client: bool) -> None:
    
    stats["total_bytes"] += size
    if from_client:
        
        stats["tx_bytes"] += size
        client.upload_bytes += size
        if PROMETHEUS_ENABLED:
            _P_TX.inc(size)
    else:
        
        stats["rx_bytes"] += size
        client.download_bytes += size
        if PROMETHEUS_ENABLED:
            _P_RX.inc(size)
    client.used_bytes = client.upload_bytes + client.download_bytes
    if (
        client.limit_bytes > 0
        and client.used_bytes >= client.limit_bytes
        and (client.used_bytes - size) < client.limit_bytes
    ):
        try:
            asyncio.get_running_loop().create_task(_enforce_quota(client.id))
        except RuntimeError:
            pass


async def _enforce_quota(client_id: str) -> None:
    
    for conn in list(proxy_connections.values()):
        if conn["client_id"] == client_id:
            await _close_ws(conn["websocket"], 1008, "Quota exceeded")


async def quota_enforcer_loop() -> None:
    
    while True:
        await asyncio.sleep(5)
        for c in list(STATE_MGR.state.clients):
            if c.limit_bytes > 0 and c.used_bytes >= c.limit_bytes:
                await _enforce_quota(c.id)


_CPU_VALUE = {"pct": 0.0, "samples": collections.deque(maxlen=72)}
_conn_speed_prev: dict[str, tuple] = {}
_client_speed_prev: dict[str, tuple] = {}


async def cpu_sample_loop() -> None:
    
    while True:
        try:
            pct = psutil.cpu_percent(interval=None)
            _CPU_VALUE["pct"] = round(pct, 1)
            _CPU_VALUE["samples"].append(pct)
        except Exception:
            pass
        await asyncio.sleep(CPU_SAMPLE_INTERVAL_SECONDS)


def _cpu_avg() -> float:
    samples = list(_CPU_VALUE["samples"])
    if not samples:
        return 0.0
    return round(sum(samples) / len(samples), 1)


def uptime_30d_pct() -> float:
    
    tracking = STATE_MGR.state.uptime_tracking
    if not tracking:
        return 0.0
    today = datetime.now(timezone.utc).date()
    total = 0.0
    for i in range(30):
        total += float(tracking.get((today - timedelta(days=i)).isoformat(), 0))
    return round(min(100.0, total / (30 * 86400) * 100.0), 1)


async def telemetry_snapshot() -> dict:
    now = time.time()
    dt = max(0.1, now - _speed["last_time"])
    d_rx = stats["rx_bytes"] - _speed["last_rx"]
    d_tx = stats["tx_bytes"] - _speed["last_tx"]
    _speed["down_mbps"] = round((d_rx * 8.0) / (dt * 1024 * 1024), 2)
    _speed["up_mbps"] = round((d_tx * 8.0) / (dt * 1024 * 1024), 2)
    _speed["last_time"] = now
    _speed["last_rx"] = stats["rx_bytes"]
    _speed["last_tx"] = stats["tx_bytes"]

    ram_mb = 0.0
    ram_total_mb = 512.0
    disk_pct = 0.0
    try:
        proc = psutil.Process()
        ram_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
        limit_mb = container_memory_limit_mb()
        if limit_mb > 0:
            ram_total_mb = limit_mb
        else:
            ram_total_mb = round(psutil.virtual_memory().total / (1024 * 1024), 0)
        try:
            du = shutil.disk_usage(STORAGE_DIR if STORAGE_DIR.exists() else BASE_DIR)
            disk_pct = round(du.used / du.total * 100, 1)
        except Exception:
            pass
    except Exception:
        pass

    conn_details = []
    for conn_id, info in list(proxy_connections.items()):
        prev = _conn_speed_prev.get(conn_id)
        cdt = max(0.1, now - (prev[0] if prev else info["started_at"]))
        c_d_rx = info["rx_bytes"] - (prev[1] if prev else 0)
        c_d_tx = info["tx_bytes"] - (prev[2] if prev else 0)
        _conn_speed_prev[conn_id] = (now, info["rx_bytes"], info["tx_bytes"])
        conn_details.append({
            "id": conn_id,
            "client_id": info["client_id"],
            "client_name": info["client_name"],
            "peer_ip": info["peer_ip"],
            "dest": info["dest"],
            "dest_port": info["dest_port"],
            "protocol": info["protocol"],
            "duration_sec": round(now - info["started_at"], 1),
            "rx_bytes": info["rx_bytes"],
            "tx_bytes": info["tx_bytes"],
            "down_bps": int(c_d_rx * 8 / cdt),
            "up_bps": int(c_d_tx * 8 / cdt),
            "geo": info.get("geo"),
        })
    for conn_id in list(_conn_speed_prev.keys()):
        if conn_id not in proxy_connections:
            _conn_speed_prev.pop(conn_id, None)

    client_speeds: dict[str, dict] = {}
    for c in STATE_MGR.state.clients:
        prev = _client_speed_prev.get(c.id)
        cdt = max(0.1, now - (prev[0] if prev else now))
        d_up = c.upload_bytes - (prev[1] if prev else c.upload_bytes)
        d_down = c.download_bytes - (prev[2] if prev else c.download_bytes)
        _client_speed_prev[c.id] = (now, c.upload_bytes, c.download_bytes)
        client_speeds[c.id] = {
            "down_bps": int(max(0, d_down) * 8 / cdt),
            "up_bps": int(max(0, d_up) * 8 / cdt),
        }
    for cid in list(_client_speed_prev.keys()):
        if cid not in {c.id for c in STATE_MGR.state.clients}:
            _client_speed_prev.pop(cid, None)

    if PROMETHEUS_ENABLED:
        _P_CONNS.set(len(proxy_connections))
        _P_CPU.set(_CPU_VALUE["pct"])
        _P_RAM.set(ram_mb)
        _P_RAM_TOTAL.set(ram_total_mb)
        _P_DISK.set(disk_pct)
        _P_CLIENT_USED.clear()
        _P_CLIENT_LIMIT.clear()
        for c in STATE_MGR.state.clients:
            _P_CLIENT_USED.labels(c.id, c.name).set(c.used_bytes)
            _P_CLIENT_LIMIT.labels(c.id, c.name).set(c.limit_bytes)

    
    _conns_minute_log[int(now // 60)] = len(proxy_connections)
    while len(_conns_minute_log) > CONNS_HISTORY_WINDOW_MINUTES:
        _conns_minute_log.pop(min(_conns_minute_log), None)
    conns_buckets = sorted(_conns_minute_log)
    conns_start = conns_buckets[0] * 60 if conns_buckets else int(now)
    conns_counts = [
        _conns_minute_log.get(b, 0)
        for b in range(conns_buckets[0], conns_buckets[-1] + 1)
    ] if conns_buckets else []

    return {
        "connections": len(proxy_connections),
        "connectionDetails": conn_details,
        "clientSpeeds": client_speeds,
        "totalRxGb": round(stats["rx_bytes"] / (1024.0 ** 3), 3),
        "totalTxGb": round(stats["tx_bytes"] / (1024.0 ** 3), 3),
        "speedDownMbps": _speed["down_mbps"],
        "speedUpMbps": _speed["up_mbps"],
        "ramMb": ram_mb,
        "ramTotalMb": ram_total_mb,
        "diskPct": disk_pct,
        "cpuPercent": _CPU_VALUE["pct"],
        "cpuAvg": _cpu_avg(),
        "uptime30d": uptime_30d_pct(),
        "gateway": gateway["status"],
        "gatewayUptimeSec": gateway_uptime_sec(),
        "connsHistoryStart": conns_start,
        "connsHistory": conns_counts,
        "peakConns24h": max(conns_counts) if conns_counts else 0,
    }


async def telemetry_loop() -> None:
    while True:
        await asyncio.sleep(TELEMETRY_INTERVAL_SECONDS)
        try:
            await dashboard_mgr.broadcast("telemetry", await telemetry_snapshot())
        except Exception:
            pass


async def persist_loop() -> None:
    last_tick = time.time()
    while True:
        await asyncio.sleep(PERSIST_INTERVAL_SECONDS)
        try:
            now = time.time()
            if gateway_running():
                today = datetime.now(timezone.utc).date().isoformat()
                STATE_MGR.state.uptime_tracking[today] = (
                    STATE_MGR.state.uptime_tracking.get(today, 0.0) + (now - last_tick)
                )
            last_tick = now
            await STATE_MGR.persist()
        except Exception:
            pass


async def _supervise(name: str, coro_factory) -> None:
    
    delay = 1.0
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Loop %s crashed (%s); restarting in %.0fs", name, exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)


async def quota_reset_loop() -> None:
    
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            changed = False
            for c in STATE_MGR.state.clients:
                if c.billing_cycle in ("monthly", "weekly") and c.next_reset_date:
                    try:
                        nxt = datetime.fromisoformat(c.next_reset_date)
                    except ValueError:
                        continue
                    if nxt.timestamp() <= now:
                        c.used_bytes = 0
                        c.upload_bytes = 0
                        c.download_bytes = 0
                        c.next_reset_date = compute_next_reset(c.billing_cycle, now)
                        changed = True
                        add_log(f"Quota reset for client '{c.name}'")
            if changed:
                await STATE_MGR.persist()
                invalidate_sub_cache()
                await broadcast_state_changed("quotaReset")
        except Exception:
            pass


async def send_alert(message: str) -> None:
    if not ALERT_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if ALERT_WEBHOOK_TYPE == "telegram":
                await client.post(
                    ALERT_WEBHOOK_URL,
                    json={
                        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
                        "text": message,
                    },
                )
            else:
                await client.post(ALERT_WEBHOOK_URL, json={"content": message})
    except Exception:
        pass


_last_alerts = {"mem": 0.0, "cpu": 0.0}


async def alert_loop() -> None:
    
    while True:
        await asyncio.sleep(60)
        try:
            proc = psutil.Process()
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            limit_mb = container_memory_limit_mb()
            mem_pct = rss_mb / limit_mb * 100.0 if limit_mb > 0 else 0.0
            cpu_avg = _cpu_avg()
            now = time.time()
            if mem_pct >= ALERT_MEM_PCT and now - _last_alerts["mem"] > ALERT_COOLDOWN_SECONDS:
                _last_alerts["mem"] = now
                await send_alert(f"⚠️ V2Leafy memory at {mem_pct:.0f}% ({rss_mb:.0f} MB)")
            if cpu_avg >= ALERT_CPU_PCT and now - _last_alerts["cpu"] > ALERT_COOLDOWN_SECONDS:
                _last_alerts["cpu"] = now
                await send_alert(f"⚠️ V2Leafy 5-min CPU avg at {cpu_avg:.0f}%")
        except Exception:
            pass


async def memory_watchdog_loop() -> None:
    
    while True:
        await asyncio.sleep(30)
        try:
            rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            limit_mb = container_memory_limit_mb()
            over = (
                MEMORY_WATCHDOG_MB > 0 and rss_mb > MEMORY_WATCHDOG_MB
            ) or (
                limit_mb > 0 and rss_mb > limit_mb * MEMORY_WATCHDOG_PCT / 100.0
            )
            if over:
                logger.warning(
                    "Memory watchdog: RSS %.0fMB exceeds threshold — restarting", rss_mb
                )
                await send_alert(
                    f"⚠️ V2Leafy memory watchdog: RSS {rss_mb:.0f} MB over threshold — restarting"
                )
                os._exit(1)
        except Exception:
            pass


GEO_CACHE: dict[str, dict] = {}


async def _geo_worker() -> None:
    
    if not GEO_LOOKUP_ENABLED:
        return
    while True:
        await asyncio.sleep(15)
        try:
            pending = [
                i for i in proxy_connections.values()
                if i["peer_ip"] and i.get("geo") is None
            ]
            if not pending:
                continue
            if len(GEO_CACHE) > 2048:
                GEO_CACHE.clear()
            async with httpx.AsyncClient(timeout=5.0) as client:
                for info in pending[:8]:
                    ip = info["peer_ip"]
                    if ip in GEO_CACHE:
                        info["geo"] = GEO_CACHE[ip]
                        continue
                    if ip.startswith(("127.", "10.", "192.168.", "169.254.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.3")) or ip in ("::1", "::"):
                        geo = {"country": None}
                        GEO_CACHE[ip] = geo
                        info["geo"] = geo
                        continue
                    try:
                        r = await client.get(
                            f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city",
                            timeout=4.0,
                        )
                        d = r.json()
                        if d.get("status") == "success":
                            geo = {
                                "country": d.get("country"),
                                "code": d.get("countryCode"),
                                "city": d.get("city"),
                            }
                        else:
                            geo = {"country": None}
                    except Exception:
                        geo = {"country": None}
                    GEO_CACHE[ip] = geo
                    info["geo"] = geo
        except Exception:
            pass







class DashboardConnection:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_DASHBOARD_QUEUE)
        self.last_ack = time.time()
        self.unhealthy = False
        self._hb_id = 0


class DashboardManager:
    def __init__(self):
        self.connections: dict[int, DashboardConnection] = {}
        self.sequence = 0
        self.lock = asyncio.Lock()

    def next_seq(self) -> int:
        self.sequence += 1
        return self.sequence

    async def register(self, conn: DashboardConnection) -> None:
        async with self.lock:
            self.connections[id(conn)] = conn

    async def unregister(self, conn: DashboardConnection) -> None:
        async with self.lock:
            self.connections.pop(id(conn), None)

    def _enqueue(self, conn: DashboardConnection, event_type: str, payload, critical: bool) -> None:
        item = (event_type, payload)
        try:
            conn.queue.put_nowait(item)
        except asyncio.QueueFull:
            if critical:
                try:
                    conn.queue.get_nowait()          
                except asyncio.QueueEmpty:
                    pass
                try:
                    conn.queue.put_nowait(item)
                except asyncio.QueueFull:
                    conn.unhealthy = True
            

    async def broadcast(self, event_type: str, payload, critical: bool = False) -> None:
        conns = list(self.connections.values())
        for conn in conns:
            self._enqueue(conn, event_type, payload, critical)


async def broadcast_state_changed(reason: str = "change") -> None:
    await dashboard_mgr.broadcast(
        "state_changed", {"state": STATE_MGR.snapshot(), "reason": reason}, critical=True
    )


async def _dash_sender(conn: DashboardConnection, manager: DashboardManager) -> None:
    try:
        while True:
            event_type, payload = await conn.queue.get()
            await conn.websocket.send_text(
                orjson.dumps(
                    {"type": event_type, "sequence": manager.next_seq(), "payload": payload},
                    default=str,
                ).decode()
            )
    except Exception:
        pass


async def _dash_heartbeat(conn: DashboardConnection, manager: DashboardManager) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if time.time() - conn.last_ack > HEARTBEAT_TIMEOUT_SECONDS:
                try:
                    await conn.websocket.close(code=1001, reason="Heartbeat timeout")
                except Exception:
                    pass
                break
            conn._hb_id += 1
            manager._enqueue(conn, "heartbeat", {"id": conn._hb_id}, critical=True)
    except Exception:
        pass


dashboard_mgr = DashboardManager()


def _origin_host(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    try:
        parsed = urlparse(v if "://" in v else f"//{v}")
        return (parsed.hostname or "").lower() or None
    except Exception:
        return None


def origin_allowed(origin: Optional[str], server_host: Optional[str] = None) -> bool:
    if not origin:
        return True
    host = _origin_host(origin)
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    expected = _origin_host(PLATFORM_CTX.public_base_url)
    if expected and host == expected:
        return True
    if server_host:
        sh = _origin_host(server_host)
        if sh and host == sh:
            return True
    if (
        host.endswith(".up.railway.app")
        or host.endswith(".railway.app")
        or host.endswith("app.github.dev")
    ):
        return True
    return False


def platform_payload(ctx: Optional[PlatformContext] = None) -> dict:
    ctx = ctx or PLATFORM_CTX
    return {
        "id": ctx.platform.value,
        "name": ctx.display_name,
        "isCodespaces": ctx.is_codespaces,
        "persistenceMode": ctx.persistence_mode,
        "publicBaseUrl": ctx.public_base_url,
        "transport": "WebSocket",
        "wsPath": "/ws/{client_id}",
        "capabilities": {
            "websocketGateway": ctx.capabilities.websocket_gateway,
            "subscriptionLinks": ctx.capabilities.subscription_links,
            "relayTools": ctx.capabilities.relay_tools,
            "persistentStorage": ctx.capabilities.persistent_storage,
            "codespacesMetadata": ctx.capabilities.codespaces_metadata,
        },
    }


def theme_payload(theme: Optional[ThemeTokens] = None) -> dict:
    theme = theme or PLATFORM_CTX.theme
    return {
        "accent": theme.accent,
        "accentHover": theme.accent_hover,
        "accentBackground": theme.accent_background,
        "success": theme.success,
    }


async def expose_codespace_port() -> None:
    
    if not PLATFORM_CTX.is_codespaces:
        return
    name = os.environ.get("CODESPACE_NAME", "")
    if not name:
        return
    port = PLATFORM_CTX.bind_port
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "codespace", "ports", "visibility",
            f"{port}:public", "-c", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=20)
    except Exception:
        pass





@asynccontextmanager
async def lifespan(app: FastAPI):
    await STATE_MGR.init()
    add_log(f"{APP_TITLE} gateway listening on port {PLATFORM_CTX.bind_port}")
    add_log(f"Platform: {PLATFORM_CTX.display_name} ({PLATFORM_CTX.platform.value})")
    add_log(f"Persistence: {PLATFORM_CTX.persistence_mode}")
    add_log("Transport: VLESS over WebSocket only")
    if UDP_FORWARDING_ENABLED:
        add_log("UDP forwarding: enabled (note: Railway egress UDP is typically blocked)")
    if PADDING_MAX > 0:
        add_log(f"Handshake padding: enabled (max {PADDING_MAX} bytes)")

    background = {
        "telemetry": lambda: telemetry_loop(),
        "persist": lambda: persist_loop(),
        "quota_enforcer": lambda: quota_enforcer_loop(),
        "quota_reset": lambda: quota_reset_loop(),
        "cpu_sampler": lambda: cpu_sample_loop(),
        "geo": lambda: _geo_worker(),
        "alerts": lambda: alert_loop(),
        "watchdog": lambda: memory_watchdog_loop(),
    }
    tasks = [
        asyncio.create_task(_supervise(name, factory))
        for name, factory in background.items()
    ]
    tasks.append(asyncio.create_task(session_cleanup_loop()))
    tasks.append(asyncio.create_task(expose_codespace_port()))
    yield
    for t in tasks:
        t.cancel()
    TCP_POOL.close_all()
    await STATE_MGR.persist()
    add_log(f"{APP_TITLE} gateway stopped")






class ORJSONResponse(JSONResponse):
    

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, default=str)


class BrotliMiddleware:
    

    def __init__(self, app, minimum_size: int = 500, compresslevel: int = 5):
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        accept = headers.get(b"accept-encoding", b"").decode("latin-1", "ignore")
        if "br" not in accept:
            return await self.app(scope, receive, send)
        response_started = {}
        response_body = bytearray()
        send_orig = send

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status = message["status"]
                resp_headers = [
                    (k.lower(), v) for k, v in message.get("headers", [])
                ]
                ctype = dict(resp_headers).get(b"content-type", b"").decode("latin-1", "ignore")
                already = dict(resp_headers).get(b"content-encoding", b"")
                if (
                    status == 200
                    and not already
                    and (ctype.startswith("text/") or ctype in ("application/json", "application/javascript"))
                    and int(dict(resp_headers).get(b"content-length", b"0") or 0) >= self.minimum_size
                ):
                    response_started["pending"] = message
                    return
            elif response_started.get("pending") and message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                if not message.get("more_body"):
                    start = response_started.pop("pending")
                    body = bytes(response_body)
                    if len(body) >= self.minimum_size:
                        body = brotli.compress(body, quality=self.compresslevel)
                        hdrs = [
                            (k, v) for k, v in start.get("headers", [])
                            if k.lower() not in (b"content-length", b"content-encoding")
                        ]
                        hdrs.append((b"content-encoding", b"br"))
                        hdrs.append((b"content-length", str(len(body)).encode()))
                        if not any(k.lower() == b"vary" for k, _ in hdrs):
                            hdrs.append((b"vary", b"Accept-Encoding"))
                        await send_orig({
                            "type": "http.response.start",
                            "status": start["status"],
                            "headers": hdrs,
                        })
                    else:
                        await send_orig(start)
                    await send_orig({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send_orig(message)

        await self.app(scope, receive, send_wrapper)


app = FastAPI(
    title=APP_TITLE,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(BrotliMiddleware, minimum_size=500)
INDEX_HTML_CACHE: Optional[str] = None


@app.middleware("http")
async def body_limit_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_HTTP_BODY_BYTES:
            return JSONResponse(
                {"ok": False, "error": "Request body too large"}, status_code=413
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    
    request.state.csp_nonce = secrets.token_urlsafe(16)
    csp = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{request.state.csp_nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Server"] = "V2Leafy"
    if _cookie_secure(request):
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path.startswith("/sub/") or request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    if getattr(request.state, "rotated_session", None):
        _set_session_cookie(request, response, request.state.rotated_session)
    return response


app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)


from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    
    detail = exc.detail if isinstance(exc.detail, str) else "Not Found"
    return JSONResponse({"ok": False, "error": detail}, status_code=exc.status_code)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        {"ok": False, "error": "Request could not be processed"}, status_code=500
    )


def get_raw_index_html() -> str:
    global INDEX_HTML_CACHE
    if INDEX_HTML_CACHE:
        return INDEX_HTML_CACHE
    try:
        INDEX_HTML_CACHE = INDEX_HTML_PATH.read_text(encoding="utf-8")
    except Exception:
        INDEX_HTML_CACHE = _FALLBACK_HTML
    return INDEX_HTML_CACHE


def serve_index_html(request: Request) -> HTMLResponse:
    token = request.cookies.get(SESSION_COOKIE)
    is_auth = session_valid_sync(token)

    ctx = PLATFORM_CTX
    theme = ctx.theme
    content = get_raw_index_html()
    content = content.replace("{{PASS_SETUP}}", "true" if STATE_MGR.state.auth.pass_setup else "false")
    content = content.replace("{{LOGGED_IN}}", "true" if is_auth else "false")
    content = content.replace("{{APP_TITLE}}", APP_TITLE)
    content = content.replace("{{THEME_ACCENT}}", theme.accent)
    content = content.replace("{{THEME_ACCENT_URL}}", theme.accent.lstrip("#"))
    content = content.replace("{{THEME_ACCENT_HOVER}}", theme.accent_hover)
    content = content.replace("{{THEME_ACCENT_BG}}", theme.accent_background)
    content = content.replace("{{THEME_SUCCESS}}", theme.success)
    content = content.replace("{{THEME_SELECTION}}", theme.selection)
    content = content.replace("{{SHOW_CODESPACES_INFO}}", "true" if ctx.show_codespaces_info else "false")
    content = content.replace("{{CSP_NONCE}}", request.state.csp_nonce)

    bootstrap = platform_payload(ctx)
    bootstrap["csrfToken"] = _csrf_token_for(token) if is_auth else ""
    content = content.replace("{{BOOTSTRAP_JSON}}", orjson.dumps(bootstrap, default=str).decode())

    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return HTMLResponse(content=content, headers=headers)


@app.get("/")
@app.head("/")
async def root_view(request: Request):
    return serve_index_html(request)


@app.get("/login")
async def login_view(request: Request):
    return serve_index_html(request)


@app.get("/dashboard")
async def dashboard_view(request: Request):
    return serve_index_html(request)


@app.get("/index.html")
async def index_view(request: Request):
    return serve_index_html(request)


@app.get("/health")
@app.head("/health")
async def health_check():
    return {
        "status": "ok",
        "platform": PLATFORM_CTX.platform.value,
        "gateway": gateway["status"],
    }


async def _readiness() -> dict:
    checks: dict[str, str] = {}
    ok = True

    try:
        if isinstance(STATE_MGR.store, FileStateStore):
            await STATE_MGR.persist()
            loaded = await STATE_MGR.store.load()
            checks["state"] = "ok" if loaded is not None else "error"
            if loaded is None:
                ok = False
        else:
            checks["state"] = "ok"
    except Exception:
        checks["state"] = "error"
        ok = False

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", PLATFORM_CTX.bind_port), timeout=3.0
        )
        writer.close()
        checks["socket"] = "ok"
    except Exception:
        checks["socket"] = "error"
        ok = False

    try:
        usage = shutil.disk_usage(STORAGE_DIR if STORAGE_DIR.exists() else BASE_DIR)
        pct = round(usage.used / usage.total * 100, 1)
        checks["disk"] = "ok" if pct < 95 else "error"
        if pct >= 95:
            ok = False
    except Exception:
        checks["disk"] = "error"
        ok = False

    checks["gateway"] = gateway["status"]
    if gateway["status"] != GatewayStatus.RUNNING.value:
        ok = False

    return {"status": "ok" if ok else "degraded", "checks": checks}


@app.get("/health/ready")
async def health_ready():
    result = await _readiness()
    if result["status"] != "ok":
        return JSONResponse(result, status_code=503)
    return result




@app.post("/api/setup")
async def api_setup(request: Request, body: SetupRequest):
    
    if STATE_MGR.state.auth.pass_setup:
        raise HTTPException(status_code=409, detail="Password setup is already complete")
    if not _origin_check(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    ip = _client_ip(request)
    if not await check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")
    async with STATE_MGR.lock:
        if STATE_MGR.state.auth.pass_setup:
            raise HTTPException(status_code=409, detail="Password setup is already complete")
        STATE_MGR.state.auth.password_hash = hash_password(body.password)
        STATE_MGR.state.auth.pass_setup = True
        await STATE_MGR.store.save(STATE_MGR.state)

    token = await create_session()
    add_log("Admin password configured on first startup")
    resp = JSONResponse({"ok": True})
    _set_session_cookie(request, resp, token)
    return resp


@app.post("/api/login")
async def api_login(request: Request, body: LoginRequest):
    
    if not _origin_check(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    ip = _client_ip(request)
    if not await check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")
    if not verify_password(body.password, STATE_MGR.state.auth.password_hash):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = await create_session()
    add_log("Admin logged in successfully")
    resp = JSONResponse({"ok": True})
    _set_session_cookie(request, resp, token)
    return resp


@app.post("/api/logout")
async def api_logout(request: Request, _=Depends(require_csrf)):
    token = request.cookies.get(SESSION_COOKIE)
    await destroy_session(token)
    add_log("Admin logged out")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    valid = await is_valid_session(token)
    return {
        "authenticated": valid,
        "pass_setup": STATE_MGR.state.auth.pass_setup,
        "csrf_token": _csrf_token_for(token) if valid else "",
    }




@app.get("/metrics")
async def metrics_endpoint():
    if not PROMETHEUS_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")
    await telemetry_snapshot()
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/state")
async def get_panel_state(_=Depends(require_auth)):
    return {
        "ok": True,
        "state": STATE_MGR.snapshot(),
        "platform": platform_payload(),
        "telemetry": await telemetry_snapshot(),
        "gateway": gateway["status"],
        "portDomain": public_host(),
        "webDomain": public_host(),
        "logs": "\n".join(console_logs),
        "uptime30d": uptime_30d_pct(),
    }


@app.put("/api/state")
@app.post("/api/state")
async def update_panel_state(request: Request, _=Depends(require_csrf)):
    raw = await request.json()
    payload = raw.get("state") if isinstance(raw.get("state"), dict) else raw
    try:
        body = StateUpdateBody.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid state payload: {exc}")
    reason = str(raw.get("reason") or "sync")[:60]

    async with STATE_MGR.lock:
        changed = False
        if body.clients is not None:
            existing_map = {c.id: c for c in STATE_MGR.state.clients}
            updated = []
            for raw_client in body.clients[:MAX_CLIENTS]:
                cid = require_valid_uuid(raw_client.id or generate_uuid())
                existing = existing_map.get(cid)
                limit = max(0.0, float(raw_client.limit))
                used_bytes = (
                    int(existing.used_bytes) if existing
                    else int(float(raw_client.usage) * (1024.0 ** 3))
                )
                status = 1 if raw_client.status else 0
                updated.append(ClientState(
                    id=cid,
                    name=sanitize_client_name(raw_client.name),
                    limit=limit,
                    limit_bytes=int(limit * (1024.0 ** 3)),
                    used_bytes=used_bytes,
                    upload_bytes=existing.upload_bytes if existing else used_bytes,
                    download_bytes=existing.download_bytes if existing else 0,
                    expiry=sanitize_text(raw_client.expiry, 40),
                    status=status,
                    active=bool(status),
                    utls=sanitize_text(raw_client.utls, 30, "chrome") or "chrome",
                    created_at=sanitize_text(
                        raw_client.created_at or (existing.created_at if existing else ""), 40
                    ),
                    ws_token=existing.ws_token if existing else "",
                    sub_slug=existing.sub_slug if existing else "",
                    billing_cycle=existing.billing_cycle if existing else QUOTA_RESET_CYCLE,
                    next_reset_date=existing.next_reset_date if existing else "",
                ))
            STATE_MGR.state.clients = updated
            backfill_client_secrets(STATE_MGR.state)
            changed = True

        if body.sub_client_subscriptions is not None:
            cleaned: dict[str, list[SubEntry]] = {}
            for cid, entries in body.sub_client_subscriptions.items():
                cleaned[sanitize_text(cid, 64)] = [
                    SubEntry(
                        id=sanitize_text(e.id or secrets.token_hex(4), 64),
                        type=e.type,
                        transport="ws",
                        name=sanitize_text(e.name, 120),
                        ipAddress=sanitize_text(e.ipAddress, 200),
                    )
                    for e in entries[:MAX_SUB_ENTRIES]
                ]
            STATE_MGR.state.sub_client_subscriptions = cleaned
            changed = True

        if body.settings is not None:
            STATE_MGR.state.settings.update(body.settings)
            changed = True

        if changed:
            await STATE_MGR.store.save(STATE_MGR.state)
            invalidate_sub_cache()

    await broadcast_state_changed(reason)
    return {"ok": True, "state": STATE_MGR.snapshot()}


@app.get("/api/config")
async def get_gateway_config(_=Depends(require_auth)):
    return {
        "ok": True,
        "config": {
            "transport": "WebSocket",
            "protocol": "VLESS",
            "path": "/ws/{client_id}",
            "publicUrl": PLATFORM_CTX.public_base_url,
            "tls": use_tls(),
            "port": PLATFORM_CTX.bind_port,
            "clients": len(STATE_MGR.state.clients),
        },
    }


@app.get("/api/sub-link")
async def get_sub_link(
    client: str = "",
    name: str = "V2Leafy Node",
    type: str = "proxy",
    ip: str = "",
    _=Depends(require_auth),
):
    
    entry_type = type if type in ("proxy", "info") else "proxy"
    if not UUID_RE.match(client or ""):
        raise HTTPException(status_code=400, detail="Invalid client identifier")
    target = next(
        (c for c in STATE_MGR.state.clients if c.id == client), None
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return {
        "ok": True,
        "link": build_single_sub_entry_link(
            PLATFORM_CTX, target, entry_type,
            sanitize_text(name, 120, "V2Leafy Node"),
            sanitize_text(ip, 200),
        ),
    }


@app.post("/api/action")
async def handle_gateway_action(request: Request, body: ActionRequest, _=Depends(require_csrf)):
    action = body.action
    if action == "start":
        gateway["status"] = GatewayStatus.RUNNING.value
        gateway["started_at"] = time.time()
        add_log("Gateway started")
        await dashboard_mgr.broadcast("gateway_status", {"status": gateway["status"]}, critical=True)
    elif action == "stop":
        gateway["status"] = GatewayStatus.STOPPED.value
        add_log("Gateway stopped")
        await close_all_proxy_connections()
        await dashboard_mgr.broadcast("gateway_status", {"status": gateway["status"]}, critical=True)
    elif action == "restart":
        gateway["status"] = GatewayStatus.RESTARTING.value
        add_log("Gateway restarting...")
        await close_all_proxy_connections()
        gateway["status"] = GatewayStatus.RUNNING.value
        gateway["started_at"] = time.time()
        add_log("Gateway restarted")
        await dashboard_mgr.broadcast("gateway_status", {"status": gateway["status"]}, critical=True)
    elif action == "clear_logs":
        console_logs.clear()
        add_log("Console logs cleared")
    await dashboard_mgr.broadcast(
        "command_result",
        {"ok": True, "action": action, "message": f"Gateway {action} executed"},
        critical=True,
    )
    return {"ok": True, "action": action}




@app.get("/api/links")
async def list_links(_=Depends(require_auth)):
    res = []
    for c in STATE_MGR.state.clients:
        res.append({
            "uuid": c.id,
            "label": c.name,
            "limit_bytes": c.limit_bytes,
            "used_bytes": c.used_bytes,
            "upload_bytes": c.upload_bytes,
            "download_bytes": c.download_bytes,
            "active": bool(c.status),
            "expiry": c.expiry,
            "created_at": c.created_at,
            "ws_token": c.ws_token,
            "sub_slug": c.sub_slug,
            "billing_cycle": c.billing_cycle,
            "next_reset_date": c.next_reset_date,
            "vless_link": generate_vless_link(
                c.id, remark=f"V2Leafy-{c.name}", ws_token=c.ws_token
            ),
        })
    return {"links": res}


@app.post("/api/links")
async def create_link_api(request: Request, body: ClientCreateRequest, _=Depends(require_csrf)):
    if len(STATE_MGR.state.clients) >= MAX_CLIENTS:
        raise HTTPException(status_code=400, detail="Client limit reached")
    limit_bytes = (
        int(body.limit_value * (1024.0 ** 3)) if body.limit_unit == "GB"
        else int(body.limit_value * (1024.0 ** 2))
    )
    cid = generate_uuid()
    is_active = True if body.active is None else bool(body.active)
    client = ClientState(
        id=cid,
        name=sanitize_client_name(body.label),
        limit=body.limit_value,
        limit_bytes=limit_bytes,
        expiry=sanitize_text(body.expiry, 40),
        status=1 if is_active else 0,
        active=is_active,
        utls="chrome",
        created_at=datetime.now().isoformat(),
        ws_token=secrets.token_urlsafe(24),
        sub_slug="token_" + secrets.token_urlsafe(16),
        billing_cycle=QUOTA_RESET_CYCLE,
        next_reset_date=compute_next_reset(QUOTA_RESET_CYCLE)
        if QUOTA_RESET_CYCLE in ("monthly", "weekly") else "",
    )
    async with STATE_MGR.lock:
        STATE_MGR.state.clients.append(client)
        await STATE_MGR.store.save(STATE_MGR.state)
        invalidate_sub_cache()
    add_log(f"Created client '{client.name}' ({cid})")
    await broadcast_state_changed("createClient")
    return {
        "ok": True,
        "uuid": cid,
        "link": generate_vless_link(cid, remark=f"V2Leafy-{client.name}", ws_token=client.ws_token),
    }


@app.patch("/api/links/{uid}")
async def patch_link_api(uid: str, request: Request, body: ClientPatchRequest, _=Depends(require_csrf)):
    uid = require_valid_uuid(uid)
    client = next((c for c in STATE_MGR.state.clients if c.id == uid), None)
    if not client:
        raise HTTPException(status_code=404, detail="Link not found")
    async with STATE_MGR.lock:
        if body.active is not None:
            client.status = 1 if body.active else 0
            client.active = bool(body.active)
        if body.label is not None:
            client.name = sanitize_client_name(body.label)
        if body.limit_value is not None:
            client.limit = body.limit_value
            client.limit_bytes = int(body.limit_value * (1024.0 ** 3))
        if body.expiry is not None:
            client.expiry = sanitize_text(body.expiry, 40)
        if body.billing_cycle is not None:
            client.billing_cycle = body.billing_cycle
            if body.billing_cycle == "none":
                client.next_reset_date = ""
            else:
                client.next_reset_date = compute_next_reset(body.billing_cycle)
        if body.reset_usage:
            client.used_bytes = 0
            client.upload_bytes = 0
            client.download_bytes = 0
        await STATE_MGR.store.save(STATE_MGR.state)
        invalidate_sub_cache()
    await broadcast_state_changed("patchClient")
    return {"ok": True}


@app.delete("/api/links/{uid}")
async def delete_link_api(uid: str, _=Depends(require_csrf)):
    uid = require_valid_uuid(uid)
    async with STATE_MGR.lock:
        STATE_MGR.state.clients = [c for c in STATE_MGR.state.clients if c.id != uid]
        STATE_MGR.state.sub_client_subscriptions.pop(uid, None)
        await STATE_MGR.store.save(STATE_MGR.state)
        invalidate_sub_cache()
    add_log(f"Deleted client {uid}")
    await broadcast_state_changed("deleteClient")
    return {"ok": True}


@app.post("/api/links/{uid}/token")
async def rotate_ws_token(uid: str, _=Depends(require_csrf)):
    uid = require_valid_uuid(uid)
    client = next((c for c in STATE_MGR.state.clients if c.id == uid), None)
    if not client:
        raise HTTPException(status_code=404, detail="Link not found")
    async with STATE_MGR.lock:
        client.ws_token = secrets.token_urlsafe(24)
        await STATE_MGR.store.save(STATE_MGR.state)
        invalidate_sub_cache()
    add_log(f"Rotated WebSocket token for client {client.name}")
    await broadcast_state_changed("rotateWsToken")
    return {"ok": True, "ws_token": client.ws_token}


@app.post("/api/links/{uid}/sub-slug")
async def regenerate_sub_slug(uid: str, _=Depends(require_csrf)):
    uid = require_valid_uuid(uid)
    client = next((c for c in STATE_MGR.state.clients if c.id == uid), None)
    if not client:
        raise HTTPException(status_code=404, detail="Link not found")
    async with STATE_MGR.lock:
        client.sub_slug = "token_" + secrets.token_urlsafe(16)
        await STATE_MGR.store.save(STATE_MGR.state)
        invalidate_sub_cache()
    add_log(f"Regenerated subscription slug for client {client.name}")
    await broadcast_state_changed("regenerateSubSlug")
    return {"ok": True, "sub_slug": client.sub_slug}


@app.get("/api/links/{uid}/sub")
async def get_single_link_subscription(uid: str, _=Depends(require_auth)):
    uid = require_valid_uuid(uid)
    client = next((c for c in STATE_MGR.state.clients if c.id == uid), None)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return {
        "ok": True,
        "subscription_url": f"{PLATFORM_CTX.public_base_url}/sub/{client.sub_slug or client.id}",
        "config": generate_vless_link(
            client.id, remark=f"V2Leafy-{client.name}", ws_token=client.ws_token
        ),
        "label": client.name,
        "used_bytes": client.used_bytes,
        "limit_bytes": client.limit_bytes,
        "sub_slug": client.sub_slug,
    }




@app.get("/api/sub/link/{client_id}")
async def get_subscription_link_url(client_id: str):
    client_id = require_valid_uuid(client_id)
    client = next((c for c in STATE_MGR.state.clients if c.id == client_id), None)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return {
        "ok": True,
        "link": f"{PLATFORM_CTX.public_base_url}/sub/{client.sub_slug or client.id}",
    }


def _expiry_epoch(expiry: str) -> int:
    
    if not expiry:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(expiry[:19].rstrip("."), fmt).timestamp())
        except (ValueError, TypeError):
            continue
    return 0


def _resolve_sub_client(identifier: str) -> Optional[ClientState]:
    
    clean_id = str(identifier).strip()
    raw_id = _b64url_decode(clean_id).strip()
    for c in STATE_MGR.state.clients:
        if (
            c.id == clean_id
            or c.id == raw_id
            or (c.sub_slug and c.sub_slug == clean_id)
            or (c.sub_slug and c.sub_slug == raw_id)
        ):
            return c
    return None


def _b64url_decode(s: str) -> str:
    try:
        padded = s + "=" * ((4 - len(s) % 4) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode(errors="ignore")
    except Exception:
        return s


SUB_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="{{SUB_ACCENT}}">
    <title>{{APP_TITLE}} Subscription Profile</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23{{THEME_ACCENT_URL}}'%3E%3Cpath d='M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z'/%3E%3C/svg%3E">
    <link rel="stylesheet" href="/static/vendor/fonts/fonts.css" integrity="sha384-1YaEs9QmiM1pCGyQWzfPxbj44relzAZqGyr0M5SHcOcpF+kbMwMPZGv6dMQ8gtIX" crossorigin="anonymous">
    <link rel="stylesheet" href="/static/vendor/fontawesome/css/all.min.css" integrity="sha384-iw3OoTErCYJJB9mCa8LNS2hbsQ7M3C0EpIsO/H5+EGAkPGc6rk+V8i04oW/K5xq0" crossorigin="anonymous">
    <script defer src="/static/vendor/js/qrcode.min.js" integrity="sha384-3zSEDfvllQohrq0PHL1fOXJuC/jSOO34H46t6UQfobFOmxE5BpjjaIJY5F2/bMnU" crossorigin="anonymous"></script>
    <style>
        :root { --bg-base: #09090b; --bg-panel: #121214; --bg-hover: #1f1f22; --border: rgba(255,255,255,0.08); --border-hover: rgba(255,255,255,0.15); --text-main: #fafafa; --text-muted: #a1a1aa; --accent: {{SUB_ACCENT}}; --accent-hover: {{SUB_ACCENT_HOVER}}; --accent-bg: {{SUB_ACCENT_BG}}; --danger: #ef4444; --warning: #f59e0b; --success: {{SUB_SUCCESS}}; --info: #3b82f6; --radius-md: 16px; --radius-sm: 10px; }
        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; -webkit-tap-highlight-color: transparent; }
        body { background: var(--bg-base); color: var(--text-main); font-family: 'Plus Jakarta Sans', sans-serif; margin: 0; padding: 24px 16px; display: flex; justify-content: center; min-height: 100vh; box-sizing: border-box; }
        .container { max-width: 480px; width: 100%; display: flex; flex-direction: column; gap: 20px; padding-bottom: 30px; }
        .card { background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,0.4); }
        .card-title { margin: 0 0 16px 0; font-size: 1.15rem; font-weight: 800; display: flex; align-items: center; gap: 10px; }
        .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .stat-box { background: var(--bg-base); border: 1px solid var(--border); padding: 14px; border-radius: var(--radius-sm); }
        .stat-label { font-size: 0.75rem; color: var(--text-muted); font-weight: 700; text-transform: uppercase; margin-bottom: 6px; letter-spacing: 0.05em; }
        .stat-val { font-size: 1.15rem; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
        .tag { padding: 4px 12px; border-radius: 8px; font-size: 0.7rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; }
        .btn { width: 100%; background: var(--bg-hover); color: var(--text-main); border: 1px solid var(--border); padding: 14px; border-radius: var(--radius-sm); font-size: 0.9rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; font-family: inherit; transition: all 0.2s ease; margin-top: 12px; }
        .btn:hover { background: var(--border-hover); transform: translateY(-1px); }
        .btn-primary { background: var(--accent); color: #000; border: none; }
        .btn-primary:hover { background: var(--accent-hover); color: #fff; }
        .btn-icon { width: 40px; height: 40px; padding: 0; margin: 0; }
        .link-item { background: var(--bg-base); border: 1px solid var(--border); padding: 14px; border-radius: var(--radius-sm); display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; transition: border-color 0.2s; }
        .link-item:hover { border-color: var(--border-hover); }
        .link-item-title { font-size: 0.9rem; font-weight: 700; margin-bottom: 4px; }
        .link-item-sub { font-size: 0.75rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; }
        .progress-bar { width: 100%; height: 8px; background: var(--bg-hover); border-radius: 4px; margin-top: 10px; overflow: hidden; }
        .progress-fill { height: 100%; background: var(--success); border-radius: 4px; transition: width 0.3s ease; }
        .progress-fill.warning { background: var(--warning); }
        .progress-fill.danger { background: var(--danger); }
        .qr-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(8px); justify-content: center; align-items: center; z-index: 100; padding: 20px; animation: fadeIn 0.2s ease; }
        .qr-modal.show { display: flex; }
        .qr-card { background: #fff; padding: 24px; border-radius: var(--radius-md); text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.5); }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        .text-accent { color: var(--accent) !important; }
        .text-info { color: var(--info) !important; }
        .text-warning { color: var(--warning) !important; }
        .import-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 12px; }
        .btn-import { background: var(--bg-base); border: 1px solid var(--border); color: var(--text-main); text-decoration: none; padding: 14px 10px; border-radius: var(--radius-sm); font-size: 0.85rem; font-weight: 700; text-align: center; transition: all 0.2s; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; }
        .btn-import:hover { background: var(--bg-hover); border-color: var(--accent); transform: translateY(-2px); }
        .btn-import i { font-size: 1.5rem; }
        .footer { text-align: center; margin-top: 20px; font-size: 0.8rem; color: var(--text-muted); font-weight: 600; }
        .footer a { color: var(--text-muted); text-decoration: none; transition: color 0.2s; }
        .footer a:hover { color: var(--text-main); }
    </style>
</head>
<body>
    <div class="container" id="app"></div>
    <div class="qr-modal" id="qr-modal" data-action="closeQr">
        <div class="qr-card" data-action="stopProp">
            <div id="qrcode" style="display:inline-block; padding:10px; border:4px solid #f0f0f0; border-radius:12px; background:#fff;"></div>
            <button class="btn" data-action="closeQr" style="margin-top:20px; background:#f4f4f5; color:#18181b; border:none;">Close QR</button>
        </div>
    </div>
    <script nonce="{{CSP_NONCE}}">
        const DATA = JSON.parse(atob('{{SUB_DATA_B64}}'));
        function fmtGB(v){ return !v ? '∞' : v.toFixed(2)+' GB'; }
        function fmtDate(d){ return !d ? 'Never' : new Date(d).toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'}); }
        function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
        function cp(t){ navigator.clipboard.writeText(t).then(()=>{ const el=document.createElement('div'); el.innerText='Copied!'; el.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--success);color:#fff;padding:10px 20px;border-radius:20px;font-weight:700;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,0.35);'; document.body.appendChild(el); setTimeout(()=>el.remove(),2000); }); }
        function qr(t){ document.getElementById('qrcode').innerHTML=''; new QRCode(document.getElementById('qrcode'),{text:t,width:220,height:220,colorDark:"#000000",colorLight:"#ffffff",correctLevel:QRCode.CorrectLevel.M}); document.getElementById('qr-modal').classList.add('show'); }
        function render(){
            const u = DATA.client.usage||0; const l = DATA.client.limit||0; const p = l>0?Math.min(100,(u/l)*100):0;
            const cls = p>90?'danger':(p>75?'warning':'');
            const subUrl = encodeURIComponent(window.location.href);
            const subName = encodeURIComponent(DATA.client.name);
            const b64Url = btoa(window.location.href);
            document.getElementById('app').innerHTML = `
                <div style="text-align:center; margin-bottom:8px;">
                    <svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:56px; height:56px; margin-bottom:12px; color:var(--accent);" aria-hidden="true"><path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12" fill="none"/></svg>
                    <h1 style="margin:0; font-size:1.8rem; font-weight:800; letter-spacing:-0.03em;">{{APP_TITLE}}</h1>
                    <p style="color:var(--text-muted); font-size:0.85rem; font-weight:600; margin-top:6px;">Subscription Environment</p>
                </div>
                <div class="card">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
                        <h2 class="card-title" style="margin:0;"><i class="fa-solid fa-user-shield text-accent"></i> ${DATA.client.name}</h2>
                        <span class="tag" style="background:${DATA.client.status?'var(--success)':'var(--danger)'}20; color:${DATA.client.status?'var(--success)':'var(--danger)'};">${DATA.client.status?'ACTIVE':'DISABLED'}</span>
                    </div>
                    <div class="stat-grid">
                        <div class="stat-box"><div class="stat-label">Used Data</div><div class="stat-val">${u>0?u.toFixed(2):'0'} GB</div></div>
                        <div class="stat-box"><div class="stat-label">Total Quota</div><div class="stat-val">${fmtGB(l)}</div></div>
                        <div class="stat-box" style="grid-column:1/-1;">
                            <div style="display:flex; justify-content:space-between; align-items:center;"><span class="stat-label" style="margin:0;">Consumption</span><span style="font-size:0.8rem; font-weight:800;">${p.toFixed(1)}%</span></div>
                            <div class="progress-bar"><div class="progress-fill ${cls}" style="width:${p}%"></div></div>
                        </div>
                        <div class="stat-box"><div class="stat-label">Expiry</div><div class="stat-val" style="font-size:0.95rem;">${fmtDate(DATA.client.expiry)}</div></div>
                        <div class="stat-box"><div class="stat-label">Remaining</div><div class="stat-val" style="font-size:0.95rem;">${l?fmtGB(Math.max(0,l-u)):'∞'}</div></div>
                    </div>
                    <button class="btn btn-primary" data-action="cp" style="margin-top:20px;"><i class="fa-solid fa-link"></i> Copy Subscription Link</button>
                    <div style="margin-top:24px;">
                        <h3 style="font-size:0.9rem; font-weight:800; margin:0 0 10px 0;"><i class="fa-solid fa-bolt text-warning"></i> One-Click Import</h3>
                        <div class="import-grid">
                            <a href="v2rayng://install-sub?url=${subUrl}&name=${subName}" class="btn-import"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192" width="26" height="26" style="color:var(--accent);"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="12" d="M22 39.005h40.738v113.99L170 39.005"/></svg> v2rayNG</a>
                            <a href="hiddify://install-sub?url=${subUrl}&name=${subName}" class="btn-import"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="26" height="26" style="color:var(--info);"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M33.578 19.376h8.146c.43 0 .776.346.776.777v19.785c0 .43-.346.777-.776.777h-8.146a.775.775 0 0 1-.776-.774V20.153c0-.43.346-.777.776-.777m8.146-12.091c.43 0 .776.347.776.777v8.359c0 .43-.346.777-.776.777h-8.146a.775.775 0 0 1-.776-.774v-3.769zM28.06 15.31c.43 0 .776.347.776.778v23.85c0 .43-.346.777-.776.777h-8.146a.775.775 0 0 1-.776-.774V20.68zm-13.638 8.15c.43 0 .776.347.776.778v15.7c0 .43-.346.777-.776.777H6.276a.775.775 0 0 1-.776-.777V28.83zm.777 11.419h3.94"/></svg> Hiddify</a>
                            <a href="shadowrocket://add/sub://${b64Url}?title=${subName}" class="btn-import"><i class="fa-solid fa-rocket text-warning"></i> Shadowrocket</a>
                            <a href="sing-box://import-remote-profile?url=${subUrl}&name=${subName}" class="btn-import"><i class="fa-solid fa-box text-accent"></i> Sing-Box</a>
                        </div>
                    </div>
                </div>
                <div class="card">
                    <h2 class="card-title"><i class="fa-solid fa-network-wired text-accent"></i> Configurations</h2>
                    <button class="btn" style="margin-bottom:20px; background:var(--accent-bg); color:var(--accent); border:none;" data-action="cpAll"><i class="fa-solid fa-copy"></i> Copy All Configs</button>
                    <div style="display:flex; flex-direction:column;">
                        ${DATA.links.map((lnk,i)=>{
                            let n = 'Node '+(i+1); try{n=decodeURIComponent(lnk.split('#')[1]||n);}catch(e){}
                            return `<div class="link-item">
                                <div style="min-width:0; flex:1; padding-right:16px;">
                                    <div class="link-item-title">${n}</div>
                                    <div class="link-item-sub">${lnk.substring(0,32)}...</div>
                                </div>
                                <div style="display:flex; gap:8px;">
                                    <button class="btn btn-icon" data-action="qr" data-link="${esc(lnk)}"><i class="fa-solid fa-qrcode"></i></button>
                                    <button class="btn btn-icon" data-action="cp" data-link="${esc(lnk)}"><i class="fa-solid fa-copy"></i></button>
                                </div>
                            </div>`;
                        }).join('')}
                    </div>
                </div>
                <div class="footer"><svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:14px; height:14px; vertical-align:-2px; margin-right:6px; color:var(--accent);" aria-hidden="true"><path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12" fill="none"/></svg>Powered by <a href="https://github.com/Code-Leafy/V2Leafy" target="_blank" rel="noopener"><i class="fa-brands fa-github"></i> V2Leafy</a></div>
            `;
        }
        document.addEventListener('click', function(e) {
            const el = e.target && e.target.closest ? e.target.closest('[data-action]') : null;
            if (!el) return;
            const act = el.getAttribute('data-action');
            if (act === 'closeQr') { document.getElementById('qr-modal').classList.remove('show'); }
            else if (act === 'stopProp') { e.stopPropagation(); }
            else if (act === 'cp') { cp(el.getAttribute('data-link') || window.location.href); }
            else if (act === 'cpAll') { cp(DATA.links.join('\n')); }
            else if (act === 'qr') { qr(el.getAttribute('data-link')); }
        });
        render();
    </script>
</body>
</html>
"""


def render_sub_template(data_obj: dict, nonce: str = "") -> str:
    b64_json = base64.b64encode(orjson.dumps(data_obj)).decode()
    html_page = SUB_HTML_TEMPLATE.replace("{{SUB_DATA_B64}}", b64_json)
    html_page = html_page.replace("{{CSP_NONCE}}", nonce)
    theme = PLATFORM_CTX.theme
    html_page = html_page.replace("{{THEME_ACCENT_URL}}", theme.accent.lstrip("#"))
    html_page = html_page.replace("{{SUB_ACCENT}}", theme.accent)
    html_page = html_page.replace("{{SUB_ACCENT_HOVER}}", theme.accent_hover)
    html_page = html_page.replace("{{SUB_ACCENT_BG}}", theme.accent_background)
    html_page = html_page.replace("{{SUB_SUCCESS}}", theme.success)
    html_page = html_page.replace("{{APP_TITLE}}", APP_TITLE)
    return html_page


@app.get("/sub/{encoded_id}")
async def public_subscription_endpoint(encoded_id: str, request: Request):
    client = _resolve_sub_client(encoded_id)
    if not client and len(STATE_MGR.state.clients) == 1:
        client = STATE_MGR.state.clients[0]
    if not client:
        raise HTTPException(status_code=404, detail="Subscription client not found")
    if not client.status:
        raise HTTPException(status_code=403, detail="Subscription disabled")

    sub_links = await cached_client_sub_links(STATE_MGR.state, client)
    sub_content = "\r\n".join(sub_links) + "\r\n"
    encoded_payload = base64.b64encode(sub_content.encode("utf-8")).decode("ascii")

    accept_header = request.headers.get("accept", "").lower()
    user_agent = request.headers.get("user-agent", "").lower()
    is_browser = (
        ("text/html" in accept_header or "mozilla" in user_agent)
        and "raw" not in request.query_params
    )

    expire_epoch = _expiry_epoch(client.expiry)
    userinfo = (
        f"upload={int(client.upload_bytes)}; download={int(client.download_bytes)}; "
        f"total={int(client.limit_bytes)}; expire={expire_epoch}"
    )

    if is_browser:
        data_obj = {
            "client": {
                "id": client.id,
                "name": client.name,
                "usage": round(client.used_bytes / (1024.0 ** 3), 3),
                "limit": client.limit,
                "expiry": client.expiry,
                "status": client.status,
            },
            "links": sub_links,
        }
        body = render_sub_template(data_obj, getattr(request.state, "csp_nonce", ""))
        etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:16] + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304)
        return HTMLResponse(
            content=body,
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    etag = '"' + hashlib.sha256(encoded_payload.encode()).hexdigest()[:16] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": f'attachment; filename="V2Leafy_{client.name}.txt"',
        "profile-update-interval": "6",
        "subscription-userinfo": userinfo,
        "ETag": etag,
    }
    return Response(content=encoded_payload, headers=headers)






@app.websocket("/api/ws")
async def dashboard_ws(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE)
    authed = await is_valid_session(token)
    await websocket.accept()
    if not authed:
        await websocket.close(code=4401)
        return
    if not origin_allowed(
        websocket.headers.get("origin"),
        websocket.headers.get("host") or (websocket.client.host if websocket.client else None),
    ):
        await websocket.close(code=4403)
        return
    if len(dashboard_mgr.connections) >= MAX_DASHBOARD_CONNECTIONS:
        await websocket.close(code=1013)
        return

    conn = DashboardConnection(websocket)
    await dashboard_mgr.register(conn)
    sender = asyncio.create_task(_dash_sender(conn, dashboard_mgr))
    heartbeat = asyncio.create_task(_dash_heartbeat(conn, dashboard_mgr))
    try:
        await websocket.send_text(orjson.dumps({
            "type": "hello",
            "sequence": dashboard_mgr.next_seq(),
            "protocol": 1,
            "payload": {
                "platform": platform_payload(),
                "theme": theme_payload(),
                "state": STATE_MGR.snapshot(),
                "telemetry": await telemetry_snapshot(),
                "logs": list(console_logs),
                "serverTime": datetime.now().isoformat(),
            },
        }, default=str).decode())
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
            except Exception:
                continue
            mtype = data.get("type")
            if mtype == "heartbeat_ack":
                conn.last_ack = time.time()
            elif mtype == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "sequence": dashboard_mgr.next_seq(),
                    "payload": {},
                })
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        sender.cancel()
        heartbeat.cancel()
        await dashboard_mgr.unregister(conn)
        try:
            await websocket.close()
        except Exception:
            pass


FQDN_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)


def parse_vless_header(first_chunk: bytes) -> dict:
    
    total = len(first_chunk)
    if total < 24:
        raise ValueError("VLESS header too short")

    pos = 0
    version = first_chunk[pos]
    pos += 1
    if version != 0:
        raise ValueError(f"Unsupported VLESS version {version}")

    raw_uuid = first_chunk[pos:pos + 16]
    pos += 16

    addon_len = first_chunk[pos]
    pos += 1
    if addon_len > 64 or pos + addon_len > total:
        raise ValueError("Invalid VLESS addon length")
    pos += addon_len

    if pos + 3 > total:
        raise ValueError("VLESS header truncated")
    command = first_chunk[pos]
    pos += 1
    if command not in (1, 2):
        raise ValueError(f"Unsupported VLESS command {command}")

    port = int.from_bytes(first_chunk[pos:pos + 2], "big")
    pos += 2
    if not (0 < port <= 65535):
        raise ValueError("VLESS port out of range")

    addr_type = first_chunk[pos]
    pos += 1
    if addr_type == 1:
        if pos + 4 > total:
            raise ValueError("VLESS IPv4 truncated")
        address = ".".join(str(b) for b in first_chunk[pos:pos + 4])
        pos += 4
    elif addr_type == 2:
        if pos + 1 > total:
            raise ValueError("VLESS domain truncated")
        domain_len = first_chunk[pos]
        pos += 1
        if domain_len == 0 or domain_len > 253 or pos + domain_len > total:
            raise ValueError("VLESS domain length invalid")
        raw = first_chunk[pos:pos + domain_len]
        pos += domain_len
        try:
            address = raw.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("VLESS domain is not valid ascii")
        if not FQDN_RE.match(address):
            raise ValueError("VLESS domain fails RFC 1123 validation")
    elif addr_type == 3:
        if pos + 16 > total:
            raise ValueError("VLESS IPv6 truncated")
        addr_bytes = first_chunk[pos:pos + 16]
        pos += 16
        address = ":".join(f"{addr_bytes[i]:02x}{addr_bytes[i + 1]:02x}" for i in range(0, 16, 2))
    else:
        raise ValueError(f"Unknown VLESS address type {addr_type}")

    uuid_str = (
        f"{raw_uuid[:4].hex()}-{raw_uuid[4:6].hex()}-{raw_uuid[6:8].hex()}-"
        f"{raw_uuid[8:10].hex()}-{raw_uuid[10:].hex()}"
    )
    return {
        "version": version,
        "uuid": uuid_str,
        "command": command,
        "address": address,
        "port": port,
        "payload": first_chunk[pos:],
    }


CLOSE_REASONS = {
    1000: "clean close",
    1001: "going away",
    1002: "protocol error",
    1003: "unsupported data",
    1005: "no status received",
    1006: "abnormal disconnect",
    1007: "invalid frame payload data",
    1008: "policy violation (auth / quota / disabled)",
    1009: "message too big",
    1010: "mandatory extension missing",
    1011: "internal server error",
    1012: "service restart",
    1013: "try again later",
    1014: "bad gateway",
    4401: "session not authenticated",
    4403: "origin not allowed",
}


def describe_close_code(code) -> str:
    return CLOSE_REASONS.get(code, f"unknown code {code}")



def _apply_socket_opts(sock, *, keepalive: bool = True) -> None:
    
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    if keepalive:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        for opt, val in (
            (getattr(socket, "TCP_KEEPIDLE", None), 60),
            (getattr(socket, "TCP_KEEPINTVL", None), 10),
            (getattr(socket, "TCP_KEEPCNT", None), 3),
        ):
            if opt is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, val)
            except OSError:
                pass


def _ws_transport_socket(websocket: WebSocket):
    
    transport = getattr(websocket, "_transport", None)
    if transport is None:
        try:
            transport = getattr(getattr(websocket, "websocket", None), "transport", None)
        except Exception:
            transport = None
    if transport is None:
        transport = getattr(websocket, "transport", None)
    try:
        return transport.get_extra_info("socket")
    except Exception:
        return None


def _tuned_relay_buf(sock, rtt_ms: float) -> int:
    
    buf = RELAY_BUF
    try:
        rcv = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) // 2
        snd = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) // 2
        sock_buf = max(rcv, snd, RELAY_BUF_MIN)
        elapsed = max(1.0, time.time() - stats["start_time"])
        avg_bps = (stats["rx_bytes"] + stats["tx_bytes"]) * 8.0 / elapsed
        bdp = max(RELAY_BUF_MIN, int(rtt_ms / 1000.0 * avg_bps))
        buf = min(max(sock_buf, bdp), RELAY_BUF_MAX)
    except Exception:
        pass
    return buf


class RelaySender:
    

    def __init__(self, websocket: WebSocket, queue_max: int = RELAY_QUEUE_MAX):
        self.websocket = websocket
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max(2, queue_max))
        self.first = True
        self.dead = False
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                data = await self.queue.get()
                await self.websocket.send_bytes(data)
        except Exception:
            self.dead = True

    async def send(self, data: bytes, first_prefix: bool = False) -> bool:
        if self.dead:
            return False
        try:
            if first_prefix:
                data = b"\x00\x00" + data
                self.first = False
            await asyncio.wait_for(self.queue.put(data), timeout=RELAY_QUEUE_FULL_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            self.dead = True
            await _close_ws(self.websocket, 1011, "Upstream faster than client")
            return False
        except Exception:
            self.dead = True
            return False

    def stop(self) -> None:
        if not self.task.done():
            self.task.cancel()


async def ws_to_tcp(
    websocket: WebSocket,
    writer: asyncio.StreamWriter,
    client: ClientState,
    conn_info: dict,
) -> None:
    
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                conn_info["client_done"] = True
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if not data:
                continue
            if len(data) > MAX_WS_FRAME_BYTES:
                await websocket.close(code=1009, reason="Frame too large")
                break
            if not check_client_quota(client, len(data)):
                await websocket.close(code=1008, reason="Quota exceeded")
                break
            record_traffic(client, len(data), from_client=True)
            conn_info["tx_bytes"] += len(data)
            conn_info["last_activity"][0] = time.time()
            writer.write(data)
            await writer.drain()
    except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception:
        pass
    finally:
        try:
            writer.write_eof()
        except Exception:
            pass


async def tcp_to_ws(
    websocket: WebSocket,
    reader: asyncio.StreamReader,
    client: ClientState,
    conn_info: dict,
    buf_size: int,
    sender: RelaySender,
    prelude: bytes = b"",
) -> None:
    
    buf = bytearray(max(buf_size, len(prelude) + 1))
    mv = memoryview(buf)
    first = True
    try:
        if prelude:
            if not check_client_quota(client, len(prelude)):
                await websocket.close(code=1008, reason="Quota exceeded")
                return
            record_traffic(client, len(prelude), from_client=False)
            conn_info["rx_bytes"] += len(prelude)
            conn_info["last_activity"][0] = time.time()
            if not await sender.send(prelude, first_prefix=True):
                return
            first = False
        while True:
            if hasattr(reader, "readinto"):
                n = await reader.readinto(mv)
            else:
                data = await reader.read(buf_size)
                n = len(data)
                if n:
                    buf[:n] = data
            if n == 0:
                conn_info["upstream_eof"] = True
                break
            chunk = bytes(mv[:n])
            if not check_client_quota(client, len(chunk)):
                await websocket.close(code=1008, reason="Quota exceeded")
                break
            record_traffic(client, len(chunk), from_client=False)
            conn_info["rx_bytes"] += len(chunk)
            conn_info["last_activity"][0] = time.time()
            if not await sender.send(chunk, first_prefix=first):
                break
            first = False
    except (ConnectionResetError, asyncio.IncompleteReadError, asyncio.CancelledError):
        pass
    except Exception:
        pass


class TcpDialPool:
    

    def __init__(self, ttl: float = CONN_POOL_TTL, max_total: int = CONN_POOL_MAX):
        self._pool: dict[tuple, list] = {}
        self._ttl = ttl
        self._max_total = max_total
        self._count = 0

    def _purge(self) -> None:
        now = time.time()
        for key in list(self._pool.keys()):
            kept = []
            for entry in self._pool[key]:
                if now - entry["ts"] < self._ttl:
                    kept.append(entry)
                else:
                    self._count -= 1
                    try:
                        entry["writer"].close()
                    except Exception:
                        pass
            if kept:
                self._pool[key] = kept
            else:
                self._pool.pop(key, None)

    def acquire(self, host: str, port: int):
        self._purge()
        key = (host, port)
        entries = self._pool.get(key)
        if entries:
            entry = entries.pop()
            self._count -= 1
            if not entries:
                self._pool.pop(key, None)
            writer = entry["writer"]
            reader = entry["reader"]
            if not writer.is_closing() and reader.at_eof():
                return reader, writer, entry["sock"]
            try:
                writer.close()
            except Exception:
                pass
        return None

    def release(self, host: str, port: int, reader, writer, sock) -> None:
        
        if self._count >= self._max_total or writer.is_closing():
            try:
                writer.close()
            except Exception:
                pass
            return
        self._purge()
        if self._count >= self._max_total:
            try:
                writer.close()
            except Exception:
                pass
            return
        key = (host, port)
        self._pool.setdefault(key, [])
        if len(self._pool[key]) >= 4:
            try:
                writer.close()
            except Exception:
                pass
            return
        self._pool[key].append({
            "reader": reader, "writer": writer, "sock": sock, "ts": time.time(),
        })
        self._count += 1

    def close_all(self) -> None:
        for entries in self._pool.values():
            for entry in entries:
                try:
                    entry["writer"].close()
                except Exception:
                    pass
        self._pool.clear()
        self._count = 0


TCP_POOL = TcpDialPool()


class UdpRelayProtocol(asyncio.DatagramProtocol):
    

    def __init__(self, sender: RelaySender, client: ClientState, conn_info: dict):
        self.sender = sender
        self.client = client
        self.conn_info = conn_info
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not check_client_quota(self.client, len(data)):
            self.transport.close()
            return
        record_traffic(self.client, len(data), from_client=False)
        self.conn_info["rx_bytes"] += len(data)
        self.conn_info["last_activity"][0] = time.time()
        framed = struct.pack(">H", len(data)) + data
        asyncio.ensure_future(self.sender.send(framed, first_prefix=self.sender.first))

    def error_received(self, exc) -> None:
        pass


async def _udp_session(
    websocket: WebSocket,
    client: ClientState,
    addr: str,
    port: int,
    conn_info: dict,
    sender: RelaySender,
    initial_payload: bytes = b"",
) -> None:
    
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UdpRelayProtocol(sender, client, conn_info),
        remote_addr=(addr, port),
    )
    conn_info["udp_transport"] = transport
    try:
        pending = bytearray(initial_payload)
        while True:
            if not pending:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes") or (msg.get("text") or "").encode()
                if not data:
                    continue
                pending = bytearray(data)
            if len(pending) > MAX_WS_FRAME_BYTES:
                await websocket.close(code=1009, reason="Frame too large")
                break
            if not check_client_quota(client, len(pending)):
                await websocket.close(code=1008, reason="Quota exceeded")
                break
            record_traffic(client, len(pending), from_client=True)
            conn_info["tx_bytes"] += len(pending)
            conn_info["last_activity"][0] = time.time()
            malformed = False
            pos = 0
            while pos < len(pending):
                if pos + 2 > len(pending):
                    malformed = True
                    break
                dlen = struct.unpack(">H", pending[pos:pos + 2])[0]
                pos += 2
                if dlen == 0 or pos + dlen > len(pending):
                    malformed = True
                    break
                transport.sendto(bytes(pending[pos:pos + dlen]))
                pos += dlen
            if malformed:
                await websocket.close(code=1008, reason="Malformed UDP frame")
                break
            pending.clear()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        pass
    finally:
        try:
            transport.close()
        except Exception:
            pass


async def _idle_watcher(websocket: WebSocket, conn_info: dict, timeout: float) -> None:
    
    interval = min(60.0, max(1.0, timeout / 2))
    try:
        while True:
            await asyncio.sleep(interval)
            if time.time() - conn_info["last_activity"][0] > timeout:
                await _close_ws(websocket, 1008, "Idle timeout")
                return
    except asyncio.CancelledError:
        raise


async def _tunnel_pinger(websocket: WebSocket) -> None:
    
    try:
        while True:
            await asyncio.sleep(TUNNEL_PING_INTERVAL)
            try:
                await websocket.send({"type": "websocket.ping"})
            except Exception:
                return
    except asyncio.CancelledError:
        raise


_FALLBACK_BODY = (
    b"<!DOCTYPE html>\n<html>\n<head>\n<title>Welcome to nginx!</title>\n"
    b"<style>html { color-scheme: light dark; } body { width: 35em; margin: 0 auto;\n"
    b"font-family: Tahoma, Verdana, Arial, sans-serif; }</style>\n</head>\n<body>\n"
    b"<h1>Welcome to nginx!</h1>\n<p>If you see this page, the nginx web server is successfully installed and\n"
    b"working. Further configuration is required.</p>\n<p>For online documentation and support please refer to\n"
    b"<a href=\"http://nginx.org/\">nginx.org</a>.<br/>\n"
    b"Commercial support is available at\n<a href=\"http://nginx.com/\">nginx.com</a>.</p>\n"
    b"<p><em>Thank you for using nginx.</em></p>\n</body>\n</html>\n"
)


async def _close_ws(websocket: WebSocket, code: int, reason: str) -> None:
    
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), timeout=2.0)
    except Exception:
        pass


async def _serve_fallback_page(websocket: WebSocket) -> None:
    
    page = (
        b"HTTP/1.1 200 OK\r\nServer: nginx\r\nContent-Type: text/html\r\n"
        b"Content-Length: " + str(len(_FALLBACK_BODY)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + _FALLBACK_BODY
    )
    try:
        await websocket.send_bytes(b"\x00\x00" + page)
    except Exception:
        pass
    try:
        await websocket.close(code=1008, reason="Invalid request")
    except Exception:
        pass


async def close_all_proxy_connections() -> None:
    for conn in list(proxy_connections.values()):
        await _close_ws(conn["websocket"], 1001, "Gateway stopped")
        try:
            conn["writer"].close()
        except Exception:
            pass
        if conn.get("udp_transport"):
            try:
                conn["udp_transport"].close()
            except Exception:
                pass
    proxy_connections.clear()


@app.websocket("/ws/{client_id}")
@app.websocket("/ws")
async def websocket_vless_tunnel(websocket: WebSocket, client_id: str = ""):
    if not gateway_running():
        await websocket.accept()
        await websocket.close(code=1008, reason="Gateway stopped")
        return

    await websocket.accept()
    writer = None
    conn_id = None
    conn_info = None
    header = None
    client: Optional[ClientState] = None
    sender: Optional[RelaySender] = None
    extra_tasks: list = []
    pooled_entry = None
    try:
        
        ed_requested = bool(PADDING_MAX > 0 and websocket.query_params.get("ed"))

        first_msg = await asyncio.wait_for(websocket.receive(), timeout=WS_HANDSHAKE_TIMEOUT)
        if first_msg["type"] == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            await websocket.close(code=1008, reason="Empty frame")
            return
        if len(first_chunk) > MAX_WS_FRAME_BYTES:
            await websocket.close(code=1009, reason="Frame too large")
            return

        
        if ed_requested:
            if len(first_chunk) >= 4 and first_chunk[:2] == b"\x00\x00":
                first_chunk = first_chunk[4:]
            else:
                await websocket.close(code=1002, reason="Padding header mismatch")
                return

        try:
            header = parse_vless_header(first_chunk)
        except ValueError:
            
            await _serve_fallback_page(websocket)
            return

        target_uuid = (client_id or header["uuid"]).strip().lower()
        if not UUID_RE.match(target_uuid):
            await _serve_fallback_page(websocket)
            return

        client = next((c for c in STATE_MGR.state.clients if c.id == target_uuid), None)
        if client is None:
            
            await _serve_fallback_page(websocket)
            return
        if not client.active or not client.status:
            await websocket.close(code=1008, reason="Client disabled")
            return

        
        ws_token = websocket.query_params.get("token", "")
        if client.ws_token and not secrets.compare_digest(ws_token, client.ws_token):
            await _serve_fallback_page(websocket)
            return

        if header["command"] not in (1, 2):
            await websocket.close(code=1008, reason="Unsupported command")
            return
        if header["command"] == 2 and not UDP_FORWARDING_ENABLED:
            await websocket.close(code=1008, reason="UDP forwarding disabled")
            return
        if not check_client_quota(client, 0):
            await websocket.close(code=1008, reason="Quota exceeded")
            return

        conn_id = secrets.token_urlsafe(8)
        conn_info = {
            "websocket": websocket,
            "writer": None,
            "client_id": client.id,
            "client_name": client.name,
            "peer_ip": websocket.client.host if websocket.client else "",
            "dest": header["address"],
            "dest_port": header["port"],
            "protocol": "udp" if header["command"] == 2 else "tcp",
            "started_at": time.time(),
            "rx_bytes": 0,
            "tx_bytes": 0,
            "last_activity": [time.time()],
            "rtt_ms": 0.0,
            "geo": None,
            "udp_transport": None,
            "upstream_eof": False,
            "client_done": False,
            "ed": ed_requested,
        }
        proxy_connections[conn_id] = conn_info
        record_traffic(client, len(first_chunk), from_client=True)
        if not check_client_quota(client, 0):
            await websocket.close(code=1008, reason="Quota exceeded")
            return

        
        ws_sock = _ws_transport_socket(websocket)
        if ws_sock:
            _apply_socket_opts(ws_sock)

        if header["command"] == 1:
            
            pooled_entry = TCP_POOL.acquire(header["address"], header["port"])
            reader = writer = sock = None
            for attempt in (0, 1):
                dial_start = time.time()
                try:
                    if attempt == 0 and pooled_entry:
                        reader, writer, sock = pooled_entry
                        if writer.is_closing():
                            pooled_entry = None
                            reader = writer = sock = None
                            continue
                    else:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection(header["address"], header["port"]),
                            timeout=TCP_CONNECT_TIMEOUT,
                        )
                        try:
                            sock = writer.get_extra_info("socket")
                        except Exception:
                            sock = None
                        pooled_entry = None
                except (asyncio.TimeoutError, OSError, ConnectionError):
                    if attempt == 0 and pooled_entry:
                        pooled_entry = None
                        continue
                    raise ConnectionError("upstream unreachable")
                rtt_ms = (time.time() - dial_start) * 1000.0
                conn_info["writer"] = writer
                if sock:
                    _apply_socket_opts(sock)
                if header["payload"]:
                    writer.write(header["payload"])
                    await writer.drain()
                try:
                    prelude = await asyncio.wait_for(
                        reader.read(1), timeout=TCP_FIRST_BYTE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    try:
                        writer.close()
                    except Exception:
                        pass
                    if attempt == 0 and pooled_entry:
                        pooled_entry = None
                        continue
                    raise ConnectionError("upstream first byte timeout")
                if prelude == b"":
                    try:
                        writer.close()
                    except Exception:
                        pass
                    if attempt == 0 and pooled_entry:
                        pooled_entry = None
                        continue
                    raise ConnectionError("upstream closed immediately")
                break

            conn_info["rtt_ms"] = rtt_ms
            logger.info(
                "Upstream handshake %.0fms to %s:%s",
                rtt_ms, header["address"], header["port"],
                extra=_log_ctx(client_id=client.id),
            )
            if rtt_ms > 2000:
                add_log(
                    f"[WARN] Upstream latency {rtt_ms:.0f}ms > 2000ms "
                    f"to {header['address']}:{header['port']}"
                )

            buf_size = _tuned_relay_buf(sock, rtt_ms) if sock else RELAY_BUF
            sender = RelaySender(websocket)
            task_up = asyncio.create_task(ws_to_tcp(websocket, writer, client, conn_info))
            task_down = asyncio.create_task(
                tcp_to_ws(websocket, reader, client, conn_info, buf_size, sender, prelude)
            )
        else:
            
            sender = RelaySender(websocket)
            task_up = asyncio.create_task(
                _udp_session(
                    websocket, client, header["address"], header["port"],
                    conn_info, sender, header["payload"],
                )
            )
            task_down = asyncio.create_task(asyncio.sleep(0))

        extra_tasks = [
            asyncio.create_task(_idle_watcher(websocket, conn_info, TCP_IDLE_TIMEOUT)),
            asyncio.create_task(_tunnel_pinger(websocket)),
        ]

        await dashboard_mgr.broadcast(
            "connection_opened",
            {
                "client_id": client.id,
                "client_name": client.name,
                "connections": len(proxy_connections),
            },
        )
        done, pending = await asyncio.wait(
            {task_up, task_down}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    except asyncio.TimeoutError:
        try:
            await websocket.close(code=1008, reason="Handshake timeout")
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        stats["total_errors"] += 1
        add_log(f"Proxy connection error: {type(exc).__name__}: {exc}")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        for t in extra_tasks:
            t.cancel()
        if sender:
            sender.stop()
        if writer and conn_info is not None and header is not None:
            try:
                if (
                    pooled_entry
                    and conn_info.get("upstream_eof")
                    and conn_info.get("client_done")
                ):
                    TCP_POOL.release(header["address"], header["port"], reader, writer, sock)
                    writer = None
                if writer:
                    writer.close()
            except Exception:
                try:
                    writer.close()
                except Exception:
                    pass
        elif writer:
            try:
                writer.close()
            except Exception:
                pass
        if conn_id:
            info = proxy_connections.pop(conn_id, None)
            if info and client is not None:
                close_code = getattr(websocket, "close_code", None) or 1006
                logger.info(
                    "Tunnel %s closed: %s (client %s, %s rx / %s tx)",
                    info["protocol"], describe_close_code(close_code),
                    client.name, info["rx_bytes"], info["tx_bytes"],
                    extra=_log_ctx(client_id=client.id),
                )
                await dashboard_mgr.broadcast(
                    "connection_closed",
                    {
                        "client_id": client.id,
                        "client_name": client.name,
                        "connections": len(proxy_connections),
                    },
                )


@app.post("/api/connections/{conn_id}/kill")
async def kill_proxy_connection(conn_id: str, _=Depends(require_csrf)):
    
    info = proxy_connections.get(conn_id)
    if not info:
        raise HTTPException(status_code=404, detail="Connection not found")
    await _close_ws(info["websocket"], 1008, "Terminated by admin")
    proxy_connections.pop(conn_id, None)
    add_log(f"Killed connection {conn_id} ({info.get('client_name', '')})")
    return {"ok": True}






if __name__ == "__main__":
    import uvicorn

    
    loop_impl = "auto"
    try:
        import uvloop  

        uvloop.install()
        loop_impl = "uvloop"
    except ImportError:
        pass
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PLATFORM_CTX.bind_port,
        access_log=False,
        loop=loop_impl,
        ws="websockets",
        ws_per_message_deflate=True,
        ws_max_size=MAX_WS_FRAME_BYTES * 2,
        timeout_keep_alive=TCP_IDLE_TIMEOUT + 30,
        server_header=False,
    )
