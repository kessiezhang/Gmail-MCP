#!/usr/bin/env python3
"""
ghub-lite: a minimal multi-account Gmail MCP server.

Five tools, no build step, no OCR, no attachments:
  - list_accounts
  - read_emails        (one account)
  - search_emails      (across all enabled accounts)
  - mark_as_read       (one account)
  - unsubscribe_email  (parses List-Unsubscribe header)

Accounts are onboarded via the separate `add_account.py` script.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


# ---------------------------------------------------------------------------
# Config & account loading
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def config_dir() -> Path:
    raw = os.environ.get("GHUB_LITE_CONFIG_DIR", "~/.ghub-lite")
    return Path(os.path.expanduser(raw))


def accounts_file() -> Path:
    return config_dir() / "accounts.json"


@dataclass
class Account:
    id: str
    email: str
    enabled: bool
    token_path: Path
    credentials_path: Path


def load_accounts() -> list[Account]:
    path = accounts_file()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    out: list[Account] = []
    for entry in data.get("accounts", []):
        out.append(
            Account(
                id=entry["id"],
                email=entry.get("email", ""),
                enabled=entry.get("enabled", True),
                token_path=Path(os.path.expanduser(entry["token_path"])),
                credentials_path=Path(os.path.expanduser(entry["credentials_path"])),
            )
        )
    return out


def get_account(account_id: str) -> Account:
    for acc in load_accounts():
        if acc.id == account_id:
            return acc
    raise ValueError(f"Account '{account_id}' not found. Run add_account.py first.")


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------


def gmail_for(account: Account):
    """Return an authenticated Gmail API client for the given account."""
    if not account.token_path.exists():
        raise RuntimeError(
            f"No token file for '{account.id}'. Run: python add_account.py {account.id}"
        )
    creds = Credentials.from_authorized_user_file(str(account.token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            account.token_path.write_text(creds.to_json())
        else:
            raise RuntimeError(
                f"Token for '{account.id}' is invalid. Re-run add_account.py {account.id}"
            )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers: list[dict], name: str) -> str:
    name_lc = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lc:
            return h.get("value", "")
    return ""


def _decode_part(data: str) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_plain_body(payload: dict) -> str:
    """Walk the MIME tree and return the first text/plain body found."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    if mime == "text/plain" and body.get("data"):
        return _decode_part(body["data"])
    for part in payload.get("parts", []) or []:
        text = _extract_plain_body(part)
        if text:
            return text
    # Fallback: strip HTML if no plain text found
    if mime == "text/html" and body.get("data"):
        html = _decode_part(body["data"])
        return re.sub(r"<[^>]+>", "", html)
    return ""


def summarize_message(msg: dict, include_body: bool) -> dict:
    payload = msg.get("payload", {}) or {}
    headers = payload.get("headers", []) or []
    list_unsub_raw = _header(headers, "List-Unsubscribe")
    out = {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "snippet": msg.get("snippet", ""),
        "label_ids": msg.get("labelIds", []),
        "is_unread": "UNREAD" in (msg.get("labelIds") or []),
    }
    if list_unsub_raw:
        parsed = _parse_list_unsubscribe(list_unsub_raw)
        out["unsubscribe"] = {
            "urls": parsed["urls"],
            "mailto": parsed["mailto"],
            "one_click": "one-click" in (
                _header(headers, "List-Unsubscribe-Post") or ""
            ).lower(),
        }
    if include_body:
        out["body"] = _extract_plain_body(payload)
    return out


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_list_accounts() -> dict:
    accounts = load_accounts()
    rows = []
    for acc in accounts:
        has_token = acc.token_path.exists()
        rows.append(
            {
                "id": acc.id,
                "email": acc.email,
                "enabled": acc.enabled,
                "has_token": has_token,
            }
        )
    return {"accounts": rows, "config_dir": str(config_dir())}


def tool_read_emails(
    account_id: str,
    max_results: int = 20,
    query: str | None = None,
    include_body: bool = False,
) -> dict:
    acc = get_account(account_id)
    svc = gmail_for(acc)
    params: dict[str, Any] = {
        "userId": "me",
        "maxResults": max(1, min(100, int(max_results))),
    }
    if query:
        params["q"] = query
    listing = svc.users().messages().list(**params).execute()
    msg_ids = [m["id"] for m in listing.get("messages", [])]
    fmt = "full" if include_body else "metadata"
    headers_filter = (
        None
        if include_body
        else ["From", "To", "Subject", "Date", "List-Unsubscribe", "List-Unsubscribe-Post"]
    )
    emails: list[dict] = []
    for mid in msg_ids:
        kwargs: dict[str, Any] = {"userId": "me", "id": mid, "format": fmt}
        if headers_filter:
            kwargs["metadataHeaders"] = headers_filter
        msg = svc.users().messages().get(**kwargs).execute()
        emails.append(summarize_message(msg, include_body))
    return {"account": acc.id, "count": len(emails), "emails": emails}


def tool_search_emails(query: str, max_results: int = 25) -> dict:
    if not query:
        raise ValueError("query is required")
    cap = max(1, min(100, int(max_results)))
    accounts = [a for a in load_accounts() if a.enabled]
    results: list[dict] = []
    errors: list[dict] = []
    for acc in accounts:
        try:
            svc = gmail_for(acc)
            listing = (
                svc.users()
                .messages()
                .list(userId="me", q=query, maxResults=cap)
                .execute()
            )
            for m in listing.get("messages", []):
                msg = (
                    svc.users()
                    .messages()
                    .get(
                        userId="me",
                        id=m["id"],
                        format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date"],
                    )
                    .execute()
                )
                summary = summarize_message(msg, include_body=False)
                summary["account"] = acc.id
                results.append(summary)
        except Exception as exc:
            errors.append({"account": acc.id, "error": str(exc)})
    # Sort by Date header descending if parseable; otherwise leave order.
    return {
        "query": query,
        "count": len(results),
        "results": results[:cap],
        "errors": errors,
    }


def tool_mark_as_read(account_id: str, message_ids: list[str]) -> dict:
    if not message_ids:
        raise ValueError("message_ids is required and cannot be empty")
    acc = get_account(account_id)
    svc = gmail_for(acc)
    body = {"ids": message_ids, "removeLabelIds": ["UNREAD"]}
    svc.users().messages().batchModify(userId="me", body=body).execute()
    return {"account": acc.id, "marked_read": len(message_ids)}


# --- Unsubscribe ------------------------------------------------------------


def _parse_list_unsubscribe(value: str) -> dict:
    """Parse a List-Unsubscribe header. Returns {urls: [...], mailto: str|None}."""
    urls: list[str] = []
    mailto: str | None = None
    for raw in value.split(","):
        item = raw.strip()
        if item.startswith("<") and item.endswith(">"):
            item = item[1:-1]
        if item.lower().startswith("mailto:"):
            mailto = item[len("mailto:") :]
        elif item.lower().startswith("http://") or item.lower().startswith("https://"):
            urls.append(item)
    return {"urls": urls, "mailto": mailto}


def tool_unsubscribe_email(account_id: str, message_id: str) -> dict:
    """
    Read List-Unsubscribe / List-Unsubscribe-Post headers and act on them.

    - If RFC 8058 one-click is supported, POST to the URL automatically.
    - Else if a mailto: target is present, send an unsubscribe email.
    - Else return the URL/mailto so the user can act manually.
    """
    acc = get_account(account_id)
    svc = gmail_for(acc)
    msg = (
        svc.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=[
                "From",
                "Subject",
                "List-Unsubscribe",
                "List-Unsubscribe-Post",
            ],
        )
        .execute()
    )
    headers = (msg.get("payload", {}) or {}).get("headers", []) or []
    raw_unsub = _header(headers, "List-Unsubscribe")
    unsub_post = _header(headers, "List-Unsubscribe-Post")
    sender = _header(headers, "From")
    subject = _header(headers, "Subject")

    if not raw_unsub:
        return {
            "status": "no_unsubscribe_header",
            "message": "This email has no List-Unsubscribe header.",
            "from": sender,
            "subject": subject,
        }

    parsed = _parse_list_unsubscribe(raw_unsub)
    one_click = "one-click" in unsub_post.lower() if unsub_post else False

    # Strategy 1: RFC 8058 one-click POST
    if one_click and parsed["urls"]:
        url = parsed["urls"][0]
        try:
            resp = requests.post(
                url,
                data={"List-Unsubscribe": "One-Click"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            return {
                "status": "one_click_posted",
                "url": url,
                "http_status": resp.status_code,
                "from": sender,
                "subject": subject,
            }
        except Exception as exc:
            return {
                "status": "one_click_failed",
                "url": url,
                "error": str(exc),
                "fallback_urls": parsed["urls"],
                "fallback_mailto": parsed["mailto"],
            }

    # Strategy 2: mailto unsubscribe
    if parsed["mailto"]:
        addr_part = parsed["mailto"].split("?", 1)[0]
        params = {}
        if "?" in parsed["mailto"]:
            for kv in parsed["mailto"].split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k.lower()] = requests.utils.unquote(v)
        unsub_subject = params.get("subject", "unsubscribe")
        unsub_body = params.get("body", "unsubscribe")
        # Build raw RFC 5322 message
        raw_email = (
            f"To: {addr_part}\r\n"
            f"Subject: {unsub_subject}\r\n"
            f"Content-Type: text/plain; charset=UTF-8\r\n\r\n"
            f"{unsub_body}\r\n"
        )
        encoded = base64.urlsafe_b64encode(raw_email.encode("utf-8")).decode("utf-8")
        try:
            svc.users().messages().send(
                userId="me", body={"raw": encoded}
            ).execute()
            return {
                "status": "mailto_sent",
                "to": addr_part,
                "from": sender,
                "subject": subject,
            }
        except HttpError as exc:
            return {
                "status": "mailto_failed",
                "error": str(exc),
                "to": addr_part,
                "fallback_urls": parsed["urls"],
            }

    # Strategy 3: just return the URL for the user to handle manually
    return {
        "status": "manual_action_required",
        "message": "Open this URL in a browser to unsubscribe.",
        "urls": parsed["urls"],
        "from": sender,
        "subject": subject,
    }


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------

server = Server("ghub-lite")


TOOLS: list[Tool] = [
    Tool(
        name="list_accounts",
        description="List configured Gmail accounts and whether they have valid tokens.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="read_emails",
        description="Fetch recent emails from a single account. Supports an optional Gmail query.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account id from list_accounts."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "query": {"type": "string", "description": "Optional Gmail search query (e.g. 'is:unread')."},
                "include_body": {"type": "boolean", "default": False, "description": "Include the plaintext body."},
            },
            "required": ["account"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="search_emails",
        description="Search across ALL enabled accounts using Gmail query syntax.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query (e.g. 'from:boss@example.com is:unread')."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="mark_as_read",
        description="Mark one or more messages as read in a specific account.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "message_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["account", "message_ids"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="unsubscribe_email",
        description=(
            "Unsubscribe from a sender by parsing the email's List-Unsubscribe header. "
            "Will use RFC 8058 one-click POST when available, otherwise send a mailto unsubscribe, "
            "otherwise return the URL for manual action."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "message_id": {"type": "string", "description": "Gmail message id of the email to unsubscribe from."},
            },
            "required": ["account", "message_id"],
            "additionalProperties": False,
        },
    ),
]


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args = arguments or {}
    try:
        if name == "list_accounts":
            result = tool_list_accounts()
        elif name == "read_emails":
            result = tool_read_emails(
                account_id=args["account"],
                max_results=args.get("max_results", 20),
                query=args.get("query"),
                include_body=args.get("include_body", False),
            )
        elif name == "search_emails":
            result = tool_search_emails(
                query=args["query"],
                max_results=args.get("max_results", 25),
            )
        elif name == "mark_as_read":
            result = tool_mark_as_read(
                account_id=args["account"],
                message_ids=args["message_ids"],
            )
        elif name == "unsubscribe_email":
            result = tool_unsubscribe_email(
                account_id=args["account"],
                message_id=args["message_id"],
            )
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:
        result = {"error": str(exc), "tool": name}
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
