# gmail-mcp

A minimal multi-account Gmail MCP server in a single Python file.

## What it does

Five tools, one file, no TypeScript build:

| Tool | What it does |
|---|---|
| `list_accounts` | Shows configured accounts and whether their tokens are valid. |
| `read_emails` | Fetches recent emails from one account (optional Gmail query). |
| `search_emails` | Searches across **all** enabled accounts at once. |
| `mark_as_read` | Removes the `UNREAD` label from one or more messages. |
| `unsubscribe_email` | Parses the `List-Unsubscribe` header and either does an RFC 8058 one-click POST, sends a `mailto:` unsubscribe, or returns the URL for you to click. |

Multi-account is supported. There is no `send_email`, no `create_draft`, no label management — by design.

## Why this exists

The full `ghub/` server pulls in TypeScript, Express, `pdf-parse`, `mammoth`, `xlsx`, `officeparser`, and a Swift OCR build step. This version is a single Python file with five dependencies and no build.

## Setup

### 1. Install Python dependencies

```bash
cd gmail-MCP
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get Google OAuth credentials

If you already used the full `ghub` server, you can reuse the same `credentials.json` from Google Cloud. Otherwise:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create or pick a project.
2. Enable the **Gmail API**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
4. Application type: **Desktop app**. Download the JSON.
5. On the OAuth consent screen, add the scopes `gmail.modify` and `userinfo.email`, and add your Gmail address as a test user.

### 3. Onboard an account

Run once per Gmail account:

```bash
python add_account.py personal /path/to/credentials.json
```

A browser window opens, you sign in, and the script saves a token under `~/.gmail-mcp/accounts/personal/`. Repeat with a different `account_id` (e.g. `work`) for additional accounts.

To override the storage location, set `GMAIL_MCP_CONFIG_DIR` before running.

### 4. Add to your MCP client

For Claude Desktop / Cowork, add to your MCP config:

```json
{
  "mcpServers": {
    "gmail-mcp": {
      "command": "/absolute/path/to/gmail-MCP/.venv/bin/python",
      "args": ["/absolute/path/to/gmail-MCP/server.py"]
    }
  }
}
```

If you set a custom config dir during onboarding, also pass it through:

```json
"env": { "GMAIL_MCP_CONFIG_DIR": "/custom/path" }
```

## Directory layout

```
~/.gmail-mcp/
├── accounts.json          # account index (id, email, paths, enabled flag)
└── accounts/
    ├── personal/
    │   ├── credentials.json
    │   └── token.json
    └── work/
        ├── credentials.json
        └── token.json
```

## Usage examples

```jsonc
// search across all accounts for unread mail from a sender
{ "tool": "search_emails", "query": "from:boss@company.com is:unread" }

// read the 10 newest emails on the work account, with bodies
{ "tool": "read_emails", "account": "work", "max_results": 10, "include_body": true }

// mark a couple of messages as read
{ "tool": "mark_as_read", "account": "personal", "message_ids": ["18f...", "18g..."] }

// unsubscribe from a marketing email
{ "tool": "unsubscribe_email", "account": "personal", "message_id": "18f..." }
```

## Troubleshooting

- **`No token file for '<id>'`** — run `python add_account.py <id> credentials.json` again.
- **`Token is invalid`** — refresh failed; re-run `add_account.py` for that id.
- **`insufficient permissions`** — your OAuth consent screen is missing `gmail.modify`. Add it and re-onboard.
- **search returns nothing on one account** — check that account is `enabled: true` in `accounts.json` and has a token file.
