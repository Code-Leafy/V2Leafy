"""Tests for request-derived public VLESS link generation.

Run with:  python tests/test_vless_public_links.py

Covers the Railway bug where generated links pointed at localhost:8080
instead of the hosted public domain.
"""

import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def make_request(headers: dict, scheme: str = "http"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "scheme": scheme,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


UUID = "8f7ec53c-62c5-43da-8682-54622bd36775"
TOKEN = "hHG2ELlkBvdU6jx5v0tiRb19T7w9h8-m"


def test_local_context_unchanged():
    """Without proxy headers, local dev keeps its localhost context."""
    ctx = main.request_public_context(make_request({"host": "localhost:8080"}))
    check("local host stays loopback", main.public_host(ctx) == "localhost")
    check("local ctx not TLS", main.use_tls(ctx) is False)


def test_railway_request_headers_derive_public_domain():
    """The core fix: browsing via the Railway domain must produce that domain."""
    req = make_request({
        "host": "myapp.up.railway.app",
        "x-forwarded-proto": "https",
    })
    ctx = main.request_public_context(req)
    check("ctx base url is https domain", ctx.public_base_url == "https://myapp.up.railway.app",
          f"got {ctx.public_base_url!r}")
    link = main.generate_vless_link(UUID, remark="r", ctx=ctx, ws_token=TOKEN)
    check("link host is public domain", link.startswith(f"vless://{UUID}@myapp.up.railway.app:443"), link)
    check("link uses TLS", "security=tls" in link)
    check("link sni matches host", "sni=myapp.up.railway.app" in link)
    check("no localhost anywhere", "localhost" not in link)


def test_forwarded_host_preferred_and_sanitized():
    req = make_request({
        "host": "internal.svc",
        "x-forwarded-host": "evil.example, myapp.up.railway.app",
        "x-forwarded-proto": "https",
    })
    ctx = main.request_public_context(req)
    check("first forwarded host wins", main.public_host(ctx) == "evil.example",
          f"got {main.public_host(ctx)!r}")

    for bad in ("localhost:8080", "127.0.0.1", "bad host", "", "meta..host"):
        ctx = main.request_public_context(make_request({
            "host": bad,
            "x-forwarded-host": bad,
            "x-forwarded-proto": "https",
        }))
        check(f"rejected forwarded host {bad!r}", main.public_host(ctx) == main.public_host(main.PLATFORM_CTX),
              f"got {main.public_host(ctx)!r}")


def test_plain_http_port_preserved():
    req = make_request({
        "host": "node.example.com",
        "x-forwarded-proto": "http",
        "x-forwarded-port": "8080",
    })
    ctx = main.request_public_context(req)
    link = main.generate_vless_link(UUID, remark="r", ctx=ctx, ws_token=TOKEN)
    check("http port kept", "vless://%s@node.example.com:8080" % UUID in link, link)
    check("plain http has security=none", "security=none" in link)


def test_path_token_encoding():
    req = make_request({"host": "myapp.up.railway.app", "x-forwarded-proto": "https"})
    ctx = main.request_public_context(req)
    link = main.generate_vless_link(UUID, remark="r", ctx=ctx, ws_token=TOKEN)
    check("ws path + token encoded", f"path=/ws/{UUID}%3Ftoken%3D{TOKEN}" in link, link)


def test_env_domain_still_honored():
    """RAILWAY_PUBLIC_DOMAIN remains the baseline when headers are absent."""
    old = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    os.environ["RAILWAY_PUBLIC_DOMAIN"] = "env.up.railway.app"
    try:
        ctx = main.build_platform_context()
        check("env domain used", ctx.public_base_url == "https://env.up.railway.app")
    finally:
        if old is None:
            os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
        else:
            os.environ["RAILWAY_PUBLIC_DOMAIN"] = old


def test_subscription_endpoint_end_to_end():
    """Full HTTP round-trip: create a client via the API with Railway headers."""
    from fastapi.testclient import TestClient

    main.app.dependency_overrides[main.require_auth] = lambda: None
    main.app.dependency_overrides[main.require_csrf] = lambda: None
    try:
        with TestClient(main.app) as client:
            headers = {"host": "myapp.up.railway.app", "x-forwarded-proto": "https"}
            r = client.post("/api/links", json={"label": "e2e"}, headers=headers)
            check("create client ok", r.status_code == 200, r.text[:200])
            data = r.json()
            check("created link targets public domain",
                  data.get("link", "").startswith(f"vless://") and "myapp.up.railway.app:443" in data.get("link", ""),
                  data.get("link", ""))

            uid = data.get("uuid")
            r2 = client.get(f"/api/links/{uid}/sub", headers=headers)
            sub = r2.json()
            check("sub config targets public domain",
                  "myapp.up.railway.app" in sub.get("config", ""), str(sub)[:200])
            check("sub url targets public domain",
                  sub.get("subscription_url", "").startswith("https://myapp.up.railway.app/sub/"),
                  sub.get("subscription_url", ""))

            slug = sub.get("sub_slug", "")
            r3 = client.get(f"/sub/{slug}", headers=headers)
            check("public sub reachable", r3.status_code == 200, str(r3.status_code))
            decoded = base64.b64decode(r3.content).decode()
            check("subscription body has public-domain links",
                  "myapp.up.railway.app" in decoded and "localhost" not in decoded,
                  decoded[:120])
            check("subscription body is TLS",
                  "security=tls" in decoded)
    finally:
        main.app.dependency_overrides.pop(main.require_auth, None)
        main.app.dependency_overrides.pop(main.require_csrf, None)


if __name__ == "__main__":
    print("== request-derived public context ==")
    test_local_context_unchanged()
    test_railway_request_headers_derive_public_domain()
    test_forwarded_host_preferred_and_sanitized()
    test_plain_http_port_preserved()
    test_path_token_encoding()
    test_env_domain_still_honored()
    print("== end-to-end API round trip ==")
    test_subscription_endpoint_end_to_end()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
