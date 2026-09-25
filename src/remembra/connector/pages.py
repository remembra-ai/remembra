"""Server-rendered sign-in and consent pages for the connector's OAuth flow.

Plain HTML (no JavaScript), every dynamic value escaped. Each response
carries its own Content-Security-Policy: a per-response style nonce, and a
``form-action`` that allows only this origin plus — on the consent page — the
client's redirect origin, because browsers apply ``form-action`` to the
redirect that follows a form POST.
"""

from __future__ import annotations

import html
import secrets
from collections.abc import Iterable
from typing import Any

from fastapi.responses import HTMLResponse

from remembra.connector.policy import SCOPE_DESCRIPTIONS

_STYLE = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1b1b;--muted:#5f5f5f;--line:#dedbd4;--accent:#1f4f46;
--warn-bg:#fff4dc;--warn-ink:#6b4a00;--err:#9b1c1c}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1e;--ink:#ececec;--muted:#a3a3a3;--line:#333;
--accent:#7cc4b3;--warn-bg:#3a2e12;--warn-ink:#f3d58a;--err:#f19999}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:440px;margin:0 auto;padding:32px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}
h1{font-size:1.25rem;margin:0 0 4px}
p{margin:8px 0}
.muted{color:var(--muted);font-size:.9rem}
.host{font-family:ui-monospace,Menlo,monospace;font-size:.9rem;word-break:break-all}
ul{padding-left:20px;margin:8px 0}
label{display:block;font-weight:600;margin:14px 0 4px}
input[type=email],input[type=password],input[type=text]{width:100%;padding:10px 12px;border:1px solid var(--line);
border-radius:8px;background:transparent;color:var(--ink);font-size:1rem}
.check{display:flex;gap:8px;align-items:flex-start;font-weight:400;margin:6px 0}
.check small{color:var(--muted)}
.row{display:flex;gap:10px;margin-top:20px}
button{flex:1;padding:12px;border-radius:8px;border:1px solid var(--accent);font-size:1rem;font-weight:600;cursor:pointer}
.primary{background:var(--accent);color:var(--card)}
.secondary{background:transparent;color:var(--accent)}
.warn{background:var(--warn-bg);color:var(--warn-ink);border-radius:8px;padding:10px 12px;font-size:.9rem;margin:12px 0}
.error{color:var(--err);font-weight:600}
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str, *, form_action_origins: Iterable[str] = (), status_code: int = 200) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    extra = " ".join(sorted(set(form_action_origins)))
    csp = (
        "default-src 'none'; "
        f"style-src 'nonce-{nonce}'; "
        f"form-action 'self'{(' ' + extra) if extra else ''}; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    document = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        f"<title>{_e(title)}</title><style nonce='{nonce}'>{_STYLE}</style></head>"
        f"<body><main><div class='card'>{body}</div></main></body></html>"
    )
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "Content-Security-Policy": csp,
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


def _scope_list(scopes: Iterable[str]) -> str:
    items = "".join(f"<li>{_e(SCOPE_DESCRIPTIONS.get(s, s))}</li>" for s in scopes)
    return f"<ul>{items}</ul>"


def _client_block(client_name: str, redirect_host: str, loopback: bool) -> str:
    block = (
        f"<p><strong>{_e(client_name)}</strong> wants to connect to your Remembra memory.</p>"
        f"<p class='muted'>Sign-in result goes to <span class='host'>{_e(redirect_host)}</span></p>"
    )
    if loopback:
        block += (
            "<div class='warn'>This app receives the result on your own computer (localhost). "
            "Only continue if you started this connection from a program you trust on this device.</div>"
        )
    return block


def error_page(message: str, status_code: int = 400) -> HTMLResponse:
    body = (
        f"<h1>Can't connect</h1><p class='error'>{_e(message)}</p>"
        "<p class='muted'>Close this window and try connecting again from the app.</p>"
    )
    return _page("Remembra connection error", body, status_code=status_code)


def login_page(
    *,
    request_id: str,
    client_name: str,
    redirect_host: str,
    redirect_origin: str,
    loopback: bool,
    scopes: list[str],
    error: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    err = f"<p class='error' role='alert'>{_e(error)}</p>" if error else ""
    body = f"""
<h1>Sign in to Remembra</h1>
{_client_block(client_name, redirect_host, loopback)}
<p class='muted'>It is asking to:</p>
{_scope_list(scopes)}
{err}
<form method='post' action='/oauth/authorize/login' autocomplete='on'>
  <input type='hidden' name='request_id' value='{_e(request_id)}'>
  <label for='email'>Email</label>
  <input id='email' name='email' type='email' required autocomplete='username' value='{_e(email)}'>
  <label for='password'>Password</label>
  <input id='password' name='password' type='password' required autocomplete='current-password'>
  <label for='totp_code'>Two-factor code <span class='muted'>(only if you turned on 2FA)</span></label>
  <input id='totp_code' name='totp_code' type='text' inputmode='numeric' pattern='[0-9]{{6}}' maxlength='6'
         autocomplete='one-time-code'>
  <div class='row'>
    <button class='secondary' type='submit' name='decision' value='deny' formnovalidate>Cancel</button>
    <button class='primary' type='submit' name='decision' value='login'>Continue</button>
  </div>
</form>
"""
    # Cancel ends in a redirect to the client, so its origin must be allowed.
    return _page("Sign in to Remembra", body, form_action_origins=[redirect_origin], status_code=status_code)


def consent_page(
    *,
    request_id: str,
    client_name: str,
    redirect_host: str,
    redirect_origin: str,
    loopback: bool,
    scopes: list[str],
    email: str,
    projects: list[dict[str, Any]],
    agent_label: str,
    error: str | None = None,
    selected: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    chosen = set(selected or ([projects[0]["project_id"]] if projects else []))
    rows = "".join(
        "<label class='check'>"
        f"<input type='checkbox' name='project' value='{_e(p['project_id'])}'{' checked' if p['project_id'] in chosen else ''}>"
        f"<span>{_e(p['project_id'])}<br><small>{_e(p.get('memories', 0))} memories</small></span></label>"
        for p in projects
    )
    if not projects:
        rows = "<p class='muted'>You have no projects yet. Name one below.</p>"
    err = f"<p class='error' role='alert'>{_e(error)}</p>" if error else ""
    body = f"""
<h1>Allow access?</h1>
<p class='muted'>Signed in as {_e(email)}</p>
{_client_block(client_name, redirect_host, loopback)}
<p class='muted'>It will be able to:</p>
{_scope_list(scopes)}
<p class='muted'>It can never edit or delete memories. You can disconnect it any time.</p>
{err}
<form method='post' action='/oauth/authorize/consent'>
  <input type='hidden' name='request_id' value='{_e(request_id)}'>
  <label>Projects it may use</label>
  {rows}
  <label for='new_project'>Another project <span class='muted'>(optional)</span></label>
  <input id='new_project' name='new_project' type='text' maxlength='128'>
  <label for='agent_id'>Agent name for this connection</label>
  <input id='agent_id' name='agent_id' type='text' required maxlength='64' value='{_e(agent_label)}'>
  <p class='muted'>Your other agents see notes and inbox messages from this connection under this name.</p>
  <div class='row'>
    <button class='secondary' type='submit' name='decision' value='deny' formnovalidate>Deny</button>
    <button class='primary' type='submit' name='decision' value='approve'>Allow</button>
  </div>
</form>
"""
    return _page("Allow access to Remembra", body, form_action_origins=[redirect_origin], status_code=status_code)
