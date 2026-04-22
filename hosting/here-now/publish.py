#!/usr/bin/env python3
"""
here.now publish script

Handles the full 3-step publish flow:
  1. POST /api/v1/publish    — declare files, get presigned upload URLs
  2. PUT <presigned-url>     — upload each file
  3. POST <finalizeUrl>      — go live

Returns a JSON object matching the website entity schema.

Usage:
  python3 publish.py --filename index.html --content-type "text/html; charset=utf-8" [--title "My Site"] [--token <api-key>] [--slug <existing-slug>]
  echo "<html>...</html>" | python3 publish.py --filename index.html

For updates (redeploy), pass --slug to target an existing publish.
For anonymous publishes (no --token), the response includes claim_token and claim_url — surfaced in data so the agent can show them to the user.
"""

import argparse
import json
import os
import sys

from agentos import connection, returns, timeout, client


connection(
    'api',
    base_url='https://here.now/api/v1',
    auth={'type': 'api_key', 'header': {'Authorization': '"Bearer " + .auth.key'}},
    label='API Key',
    help_url='https://here.now',
    optional=True)


BASE_URL = "https://here.now/api/v1"


def _map_website(w: dict) -> dict:
    viewer = w.get("viewer") or {}
    return {
        "id": w.get("slug"),
        "name": viewer.get("title") or w.get("slug"),
        "url": w.get("siteUrl"),
        "status": "active" if w.get("status") == "active" else "pending",
        "published": w.get("updatedAt"),
        "versionId": w.get("currentVersionId"),
        "expiresAt": w.get("expiresAt"),
        "anonymous": w.get("anonymous", False),
        "claimToken": w.get("claimToken"),
        "claimUrl": w.get("claimUrl"),
    }


@returns("website[]")
@connection("api")
async def list_websites(**params) -> list[dict]:
    """List all your published sites (requires authentication)"""
    token = params.get("auth", {}).get("key", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = await client.get(f"{BASE_URL}/publishes", headers=headers)
    return [_map_website(w) for w in (resp["json"] or {}).get("publishes", [])]


@returns({"ok": "boolean"})
@connection("api")
async def delete_website(*, slug: str, **params) -> dict:
    """Delete a published site (requires authentication)

        Args:
            slug: The site slug to delete
        """
    token = params.get("auth", {}).get("key", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    await client.delete(f"{BASE_URL}/publish/{slug}", headers=headers)
    return {"success": True, "id": slug}


@returns({"success": "boolean", "slug": "string"})
@connection("api")
async def claim_website(*, slug: str, claim_token: str, **params) -> dict:
    """Claim an anonymous publish to make it permanent. Requires authentication. The claim_token is in entity.data.claim_token — returned once at publish time, never again.

        Args:
            slug: The site slug to claim
            claim_token: The one-time claim token from entity.data.claim_token
        """
    token = params.get("auth", {}).get("key", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    await client.post(
        f"{BASE_URL}/publish/{slug}/claim",
        json={"claimToken": claim_token}, headers=headers,
    )
    return {"success": True, "slug": slug}


@returns({"sent": "boolean", "message": "string"})
async def op_signup(*, email: str, **params) -> dict:
    """Send a magic link to the user's email. They click it, land on the here.now dashboard, and copy their API key. Then add it to AgentOS credentials for permanent publishes (no 24h expiry, 60/hour rate limit). After getting the key: POST /sys/accounts { "skill": "here-now", "account": "default", "api_key": "..." }

        Args:
            email: User's email address
        """
    await client.post("https://here.now/api/auth/login", json={"email": email})
    return {
        "sent": True,
        "message": (
            "Check your inbox for a sign-in link from here.now. "
            "Click it, then copy your API key from the dashboard "
            "and add it to AgentOS credentials for this skill."
        ),
    }


@returns({"success": "boolean"})
@connection("api")
async def patch_metadata(*, slug: str, title: str = None, description: str = None, ttl: int = None, **params) -> dict:
    """Update site title, description, or TTL without redeploying files

        Args:
            title: New title
            description: New description
            ttl: New TTL in seconds
        """
    token = params.get("auth", {}).get("key", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body: dict = {}
    if ttl is not None:
        body["ttlSeconds"] = ttl
    viewer: dict = {}
    if title:
        viewer["title"] = title
    if description:
        viewer["description"] = description
    if viewer:
        body["viewer"] = viewer
    await client.patch(
        f"{BASE_URL}/publish/{slug}/metadata",
        json=body, headers=headers,
    )
    return {"success": True}


def _make_request(url, method="GET", body=None, headers=None, content_type="application/json"):
    headers = dict(headers or {})
    kwargs: dict = {"headers": headers}

    if body is not None and isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif body is not None:
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        headers["Content-Type"] = content_type
        kwargs["data"] = body

    dispatch = {"GET": client.get, "POST": client.post, "PUT": client.put, "DELETE": client.delete, "PATCH": client.patch}
    fn = dispatch.get(method, client.get)
    resp = fn(url, **kwargs)

    if not resp.get("ok"):
        err_body = resp.get("body", "")
        try:
            err = json.loads(err_body)
        except Exception:
            err = {"error": err_body}
        print(json.dumps({"success": False, "error": f"HTTP {resp.get('status', 0)}", "detail": err}), file=sys.stderr)
        sys.exit(1)

    return resp.get("json") if resp.get("json") is not None else resp.get("body", "")


def _do_publish(
    content,
    filename="index.html",
    content_type="text/html; charset=utf-8",
    title=None,
    description=None,
    slug=None,
    token=None,
    ttl=None,
):
    """Core publish logic. Content can be str or bytes. Returns website entity dict."""
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    token = (token or "").strip()
    if not token:
        creds_path = os.path.expanduser("~/.herenow/credentials")
        try:
            with open(creds_path) as f:
                token = f.read().strip()
        except FileNotFoundError:
            pass

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    create_body = {
        "files": [
            {
                "path": filename,
                "size": len(content_bytes),
                "contentType": content_type,
            }
        ]
    }

    if title or description:
        create_body["viewer"] = {}
        if title:
            create_body["viewer"]["title"] = title
        if description:
            create_body["viewer"]["description"] = description

    if ttl and token:
        create_body["ttlSeconds"] = ttl

    if slug:
        url = f"{BASE_URL}/publish/{slug}"
        response = _make_request(url, method="PUT", body=create_body, headers=dict(headers))
    else:
        url = f"{BASE_URL}/publish"
        response = _make_request(url, method="POST", body=create_body, headers=dict(headers))

    slug_val = response.get("slug")
    site_url = response.get("siteUrl")
    upload_info = response.get("upload", {})
    uploads = upload_info.get("uploads", [])
    finalize_url = upload_info.get("finalizeUrl")
    version_id = upload_info.get("versionId")

    for upload in uploads:
        put_url = upload["url"]
        put_headers = dict(upload.get("headers", {}))
        _make_request(put_url, method="PUT", body=content_bytes, headers=put_headers, content_type=content_type)

    finalize_headers = {}
    if token:
        finalize_headers["Authorization"] = f"Bearer {token}"
    _make_request(finalize_url, method="POST", body={"versionId": version_id}, headers=finalize_headers)

    output = {
        "slug": slug_val,
        "siteUrl": site_url,
        "status": "active",
        "currentVersionId": version_id,
    }
    if title:
        output["viewer"] = {"title": title}
    if response.get("expiresAt"):
        output["expiresAt"] = response["expiresAt"]
    if response.get("anonymous"):
        output["anonymous"] = True
        output["claimToken"] = response.get("claimToken", "")
        output["claimUrl"] = response.get("claimUrl", "")

    return output


@returns("website")
@timeout(60)
async def op_create_website(
    content,
    filename="index.html",
    content_type="text/html; charset=utf-8",
    title=None,
    description=None,
    ttl=None,
    **params,
):
    """Entry point for python: executor. Create a new publish."""
    token = params.get("auth", {}).get("key", "")
    return _do_publish(
        content=content,
        filename=filename,
        content_type=content_type,
        title=title,
        description=description,
        ttl=ttl,
        token=token,
    )


@returns("website")
@timeout(60)
async def op_update_website(
    slug,
    content,
    filename="index.html",
    content_type="text/html; charset=utf-8",
    title=None,
    **params,
):
    """Entry point for python: executor. Update an existing publish."""
    token = params.get("auth", {}).get("key", "")
    return _do_publish(
        content=content,
        filename=filename,
        content_type=content_type,
        title=title,
        slug=slug,
        token=token,
    )


def _main():
    parser = argparse.ArgumentParser(description="Publish files to here.now")
    parser.add_argument("--filename", default="index.html", help="File path within the publish")
    parser.add_argument("--content-type", default="text/html; charset=utf-8", dest="content_type")
    parser.add_argument("--content", help="File content (reads from stdin if omitted)")
    parser.add_argument("--title", help="Human-readable site title")
    parser.add_argument("--description", help="Site description")
    parser.add_argument("--ttl", type=int, help="TTL in seconds (authenticated only)")
    parser.add_argument("--token", default="", help="here.now API key (omit for anonymous)")
    parser.add_argument("--slug", help="Existing slug to update (omit to create new)")
    args = parser.parse_args()

    content = args.content if args.content else sys.stdin.read()
    output = _do_publish(
        content=content,
        filename=args.filename,
        content_type=args.content_type,
        title=args.title,
        description=args.description,
        slug=args.slug,
        token=args.token or "",
        ttl=args.ttl,
    )
    print(json.dumps(output))


if __name__ == "__main__":
    _main()
