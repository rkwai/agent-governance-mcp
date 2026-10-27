"""Built-in Supabase OAuth authorization UI using public browser configuration only.

The browser talks directly to Supabase Auth.  This process serves inert HTML,
JavaScript, and CSS; it never accepts an agent account password, access token, or
authorization decision.  Supabase remains the authorization server and returns
the only redirect URL the browser will follow.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from html import escape
from urllib.parse import urlsplit

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response

CONSENT_PATH = "/oauth/consent"
CONSENT_JAVASCRIPT_PATH = "/oauth/consent/app.js"
CONSENT_STYLESHEET_PATH = "/oauth/consent/styles.css"
MAX_AUTHORIZATION_ID_LENGTH = 256
MAX_PUBLISHABLE_KEY_LENGTH = 8192
MAX_RETURN_ADDRESS_LENGTH = 4096
_CONSENT_ROUTE_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

# Standard OIDC scopes the authorization server may return. Request ``openid``;
# extra standard scopes such as Grok Bot's ``offline_access`` must not fail closed.
STANDARD_CONSENT_SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    "profile",
    "phone",
    "offline_access",
)
_STANDARD_CONSENT_SCOPE_SET = frozenset(STANDARD_CONSENT_SCOPES)

# Cursor desktop loopback callback and the Grok Bot custom-scheme callback.
LOOPBACK_HTTP_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1")
LOOPBACK_HTTP_PORT = 8787
LOOPBACK_HTTP_PATH = "/callback"
GROKBOT_RETURN_SCHEME = "grokbot"
GROKBOT_RETURN_HOST = "mcp"
GROKBOT_RETURN_PATH = "/oauth/callback"

_AUTHORIZATION_ID = re.compile(
    rf"[A-Za-z0-9_-]{{1,{MAX_AUTHORIZATION_ID_LENGTH}}}",
    flags=re.ASCII,
)
_PUBLISHABLE_KEY = re.compile(r"sb_publishable_[A-Za-z0-9_-]+", flags=re.ASCII)
_JWT_SEGMENT = re.compile(r"[A-Za-z0-9_-]+", flags=re.ASCII)


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _supabase_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("supabase_url must be a canonical HTTPS origin")
    if _contains_control_characters(value):
        raise ValueError("supabase_url must not contain control characters")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("supabase_url must be an HTTPS origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("supabase_url must not contain user information")
    if parsed.path:
        raise ValueError("supabase_url must be an origin without a path")
    if parsed.query or parsed.fragment:
        raise ValueError("supabase_url must not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("supabase_url must contain a valid optional port") from error
    del port
    return value


def _decode_legacy_jwt_segment(segment: str, *, max_decoded_bytes: int) -> object:
    if _JWT_SEGMENT.fullmatch(segment) is None:
        raise ValueError("invalid legacy JWT segment")
    padding = "=" * (-len(segment) % 4)
    try:
        decoded = base64.b64decode(
            (segment + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ValueError("invalid legacy JWT segment") from error
    if not decoded or len(decoded) > max_decoded_bytes:
        raise ValueError("invalid legacy JWT segment size")
    try:
        return json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid legacy JWT JSON") from error


def _is_legacy_anon_key(value: str) -> bool:
    segments = value.split(".")
    if len(segments) != 3 or any(not segment for segment in segments):
        return False
    try:
        header = _decode_legacy_jwt_segment(segments[0], max_decoded_bytes=1024)
        payload = _decode_legacy_jwt_segment(segments[1], max_decoded_bytes=4096)
    except ValueError:
        return False
    return (
        isinstance(header, dict)
        and header.get("alg") == "HS256"
        and header.get("typ") == "JWT"
        and isinstance(payload, dict)
        and payload.get("role") == "anon"
        and _JWT_SEGMENT.fullmatch(segments[2]) is not None
    )


def _validate_publishable_key(value: str) -> None:
    if _PUBLISHABLE_KEY.fullmatch(value) is not None:
        return
    if _is_legacy_anon_key(value):
        return
    raise ValueError("supabase_publishable_key must be an sb_publishable_ key or legacy anon JWT")


@dataclass(frozen=True, slots=True)
class ConsentSettings:
    """Validated public configuration embedded into the browser page.

    ``supabase_publishable_key`` is Supabase's public browser key.  A secret or
    service-role key is neither accepted by the interface nor needed by the flow.
    """

    supabase_url: str
    supabase_publishable_key: str = field(repr=False)

    def __post_init__(self) -> None:
        origin = _supabase_origin(self.supabase_url)
        key = self.supabase_publishable_key
        if not isinstance(key, str) or not key:
            raise ValueError("supabase_publishable_key must not be empty")
        if key != key.strip():
            raise ValueError("supabase_publishable_key must be canonical")
        if _contains_control_characters(key):
            raise ValueError("supabase_publishable_key must not contain control characters")
        if len(key) > MAX_PUBLISHABLE_KEY_LENGTH:
            raise ValueError(
                f"supabase_publishable_key must not exceed {MAX_PUBLISHABLE_KEY_LENGTH} characters"
            )
        _validate_publishable_key(key)
        object.__setattr__(self, "supabase_url", origin)


def _valid_authorization_id(value: object) -> bool:
    return isinstance(value, str) and _AUTHORIZATION_ID.fullmatch(value) is not None


def normalized_consent_scopes(value: object) -> tuple[str, ...]:
    """Accept only standard OIDC scopes the authorization server may return."""

    if isinstance(value, str):
        scopes = tuple(scope for scope in value.split() if scope)
    elif isinstance(value, (list, tuple)):
        scopes = tuple(value)
        if any(
            not isinstance(scope, str) or not scope or scope != scope.strip() for scope in scopes
        ):
            raise ValueError("unsupported scopes")
    else:
        raise ValueError("unsupported scopes")
    if not scopes or any(scope not in _STANDARD_CONSENT_SCOPE_SET for scope in scopes):
        raise ValueError("unsupported scopes")
    return scopes


def is_allowed_consent_return_address(value: object) -> bool:
    """Return whether the authorization server's redirect is a reviewed callback.

    Allowed destinations:

    * ``https:`` URLs with a hostname and no userinfo
    * loopback ``http://localhost:8787/callback`` or ``http://127.0.0.1:8787/callback``
    * exact ``grokbot://mcp/oauth/callback``
    """

    if not isinstance(value, str) or not value or len(value) > MAX_RETURN_ADDRESS_LENGTH:
        return False
    if _contains_control_characters(value):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return False
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.lower()
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http":
        return (
            hostname in LOOPBACK_HTTP_HOSTS
            and port == LOOPBACK_HTTP_PORT
            and parsed.path == LOOPBACK_HTTP_PATH
        )
    if parsed.scheme == GROKBOT_RETURN_SCHEME:
        return (
            hostname == GROKBOT_RETURN_HOST and parsed.path == GROKBOT_RETURN_PATH and port is None
        )
    return False


def _js_quoted_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _consent_javascript_allowlists() -> str:
    """Emit the browser scope set and return-address allowlist from Python constants."""

    scopes = _js_quoted_list(STANDARD_CONSENT_SCOPES)
    hosts = _js_quoted_list(LOOPBACK_HTTP_HOSTS)
    return "\n".join(
        (
            f"const standardScopes=new Set([{scopes}]);",
            f"const loopbackHttpHosts=new Set([{hosts}]);",
            "function allowedReturnAddress(destination){",
            "  if(destination.username||destination.password)return false;",
            "  if(destination.protocol==='https:')return Boolean(destination.hostname);",
            "  if(destination.protocol==='http:'){",
            "    return loopbackHttpHosts.has(destination.hostname)",
            f"      &&destination.port==='{LOOPBACK_HTTP_PORT}'",
            f"      &&destination.pathname==='{LOOPBACK_HTTP_PATH}';",
            "  }",
            f"  if(destination.protocol==='{GROKBOT_RETURN_SCHEME}:'){{",
            f"    return destination.hostname==='{GROKBOT_RETURN_HOST}'",
            f"      &&destination.pathname==='{GROKBOT_RETURN_PATH}'",
            "      &&!destination.port;",
            "  }",
            "  return false;",
            "}",
        )
    )


def security_headers(settings: ConsentSettings) -> dict[str, str]:
    """Return security and non-persistence headers for every consent asset."""

    csp = "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'none'",
            "script-src 'self'",
            "style-src 'self'",
            f"connect-src {settings.supabase_url}",
        )
    )
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Content-Security-Policy": csp,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    }


def consent_html(settings: ConsentSettings, authorization_id: str) -> str:
    """Render one authorization page after strict request-ID validation."""

    if not isinstance(settings, ConsentSettings):
        raise TypeError("settings must be ConsentSettings")
    if not _valid_authorization_id(authorization_id):
        raise ValueError("authorization_id is invalid")
    safe_origin = escape(settings.supabase_url, quote=True)
    safe_key = escape(settings.supabase_publishable_key, quote=True)
    safe_authorization_id = escape(authorization_id, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Authorize AI Nature Company</title>
  <link rel="stylesheet" href="{CONSENT_STYLESHEET_PATH}">
</head>
<body>
  <main id="app" data-supabase-url="{safe_origin}"
        data-publishable-key="{safe_key}"
        data-authorization-id="{safe_authorization_id}">
    <section class="card">
      <div class="mark" aria-hidden="true">ANC</div>
      <p class="eyebrow">AI Nature Company</p>
      <h1>Authorize this agent connector</h1>
      <p class="lede">Sign in with the pre-provisioned agent account assigned to this
        connector. Each account has a distinct Supabase subject; the substrate binds
        that subject to one agent instance, role, cell, and run.</p>
      <div id="status" class="status" role="status" aria-live="polite">
        Sign in to review this authorization request.
      </div>

      <section id="signin">
        <form id="login-form">
          <label for="email">Agent account email</label>
          <input id="email" name="email" type="email" autocomplete="username" required>
          <label for="password">Password</label>
          <input id="password" name="password" type="password"
                 autocomplete="current-password" required>
          <button id="login" class="primary" type="submit">Sign in and review</button>
        </form>
        <p class="privacy">The browser sends these values directly to Supabase Auth.
          They are never submitted to or logged by the MCP server.</p>
      </section>

      <section id="details" hidden>
        <h2>Review connection</h2>
        <dl>
          <div><dt>Client</dt><dd id="client"></dd></div>
          <div><dt>Protected resource</dt><dd id="resource"></dd></div>
          <div><dt>Standard scopes</dt><dd id="scopes"></dd></div>
          <div class="technical"><dt>Return address</dt><dd id="redirect"></dd></div>
        </dl>
        <div class="actions">
          <button id="approve" class="primary" type="button">Approve</button>
          <button id="deny" class="secondary" type="button">Deny</button>
        </div>
      </section>

      <p class="footnote">The connector receives only the identity access shown above.
        Agent authority comes from the server-owned binding, never from browser input.</p>
    </section>
  </main>
  <script type="module" src="{CONSENT_JAVASCRIPT_PATH}"></script>
</body>
</html>"""


def consent_stylesheet() -> str:
    """Return the external stylesheet without runtime configuration."""

    return """:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  color: #17211b;
  background: #edf3ee;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background:
    radial-gradient(circle at 15% 10%, #d7eadb 0, transparent 34%),
    radial-gradient(circle at 90% 90%, #dce7f4 0, transparent 32%), #f5f8f5;
}
main { width: min(100%, 620px); }
.card {
  background: rgba(255, 255, 255, .96);
  border: 1px solid #d8e1da;
  border-radius: 24px;
  padding: clamp(26px, 6vw, 48px);
  box-shadow: 0 22px 65px rgba(30, 54, 39, .13);
}
.mark {
  display: grid;
  place-items: center;
  width: 52px;
  height: 52px;
  border-radius: 15px;
  background: #173f2b;
  color: #fff;
  font-size: .82rem;
  font-weight: 850;
  letter-spacing: .02em;
}
.eyebrow {
  margin: 22px 0 8px;
  text-transform: uppercase;
  letter-spacing: .12em;
  font-size: .76rem;
  font-weight: 800;
  color: #4e705c;
}
h1 {
  font-size: clamp(2rem, 6vw, 3rem);
  line-height: 1.02;
  letter-spacing: -.045em;
  margin: 0;
  max-width: 520px;
}
h2 { font-size: 1.1rem; margin: 26px 0 12px; }
.lede {
  font-size: 1.02rem;
  line-height: 1.65;
  color: #536158;
  margin: 18px 0 22px;
}
.status {
  border-radius: 12px;
  background: #f0f5f1;
  color: #365342;
  padding: 12px 14px;
  font-size: .92rem;
  margin: 16px 0;
}
.status[data-tone=error] { background: #fff0ee; color: #8d3026; }
form { display: grid; gap: 9px; }
label { font-size: .8rem; font-weight: 750; color: #405348; margin-top: 5px; }
input {
  width: 100%;
  border: 1px solid #cbd7ce;
  border-radius: 11px;
  padding: 12px 13px;
  background: #fff;
  color: #17211b;
  font: inherit;
}
input:focus { outline: 3px solid #a9ceb3; outline-offset: 1px; }
.primary, .secondary {
  appearance: none;
  border: 0;
  border-radius: 12px;
  padding: 13px 17px;
  font: inherit;
  font-weight: 750;
  cursor: pointer;
}
.primary { background: #173f2b; color: #fff; }
.secondary { background: #eef3ef; color: #284534; }
form .primary { margin-top: 8px; }
.actions { display: flex; gap: 10px; margin-top: 22px; }
.privacy, .footnote { color: #6b776f; font-size: .82rem; line-height: 1.55; }
.privacy { margin: 12px 4px 0; }
.footnote {
  border-top: 1px solid #e3e9e4;
  margin: 28px 0 0;
  padding-top: 18px;
}
dl { margin: 0; border: 1px solid #dce5de; border-radius: 14px; overflow: hidden; }
dl div { padding: 12px 14px; border-bottom: 1px solid #e5ebe6; }
dl div:last-child { border-bottom: 0; }
dt {
  font-size: .72rem;
  text-transform: uppercase;
  letter-spacing: .08em;
  font-weight: 800;
  color: #6d7a71;
}
dd { margin: 5px 0 0; overflow-wrap: anywhere; }
.technical { font-size: .8rem; }
@media (max-width: 520px) {
  body { padding: 12px; }
  .card { border-radius: 18px; }
  .actions { flex-direction: column; }
  .primary, .secondary { width: 100%; }
}
button:focus-visible { outline: 3px solid #84b894; outline-offset: 3px; }
button:disabled { opacity: .55; cursor: wait; }
"""


def consent_javascript() -> str:
    """Return the browser flow; all account data travels browser-to-Supabase."""

    return (
        """const root=document.getElementById('app');
const status=document.getElementById('status');
const signin=document.getElementById('signin');
const details=document.getElementById('details');
const form=document.getElementById('login-form');
const passwordInput=document.getElementById('password');
const approve=document.getElementById('approve');
const deny=document.getElementById('deny');
const authorizationId=root.dataset.authorizationId||'';
const base=root.dataset.supabaseUrl;
const apiKey=root.dataset.publishableKey;
const accessKey='anc_oauth_access:'+authorizationId;
"""
        + _consent_javascript_allowlists()
        + """
let deciding=false;

const setStatus=(message,tone='')=>{status.textContent=message;status.dataset.tone=tone;};
const setText=(id,value)=>{document.getElementById(id).textContent=String(value||'Not provided');};
const clearAccess=()=>sessionStorage.removeItem(accessKey);

class FlowError extends Error{constructor(code,message){super(message);this.code=code;}}

function fail(error){
  if(error?.code==='session_expired'){
    clearAccess();
    details.hidden=true;
    signin.hidden=false;
  }
  setStatus(error?.message||'The authorization request could not be completed.','error');
}

function normalizedScopes(value){
  const scopes=Array.isArray(value)?value:String(value||'').split(/\\s+/).filter(Boolean);
  if(!scopes.length||scopes.some(scope=>!standardScopes.has(scope))){
    throw new FlowError('invalid_scopes','The authorization server returned unsupported scopes.');
  }
  return scopes;
}

function finish(result,message){
  if(!result||typeof result.redirect_url!=='string'||result.redirect_url.length>4096){
    throw new FlowError(
      'missing_redirect',
      'The authorization server returned no valid return address.'
    );
  }
  let destination;
  try{destination=new URL(result.redirect_url);}catch{
    throw new FlowError(
      'invalid_redirect',
      'The authorization server returned an invalid return address.'
    );
  }
  if(!allowedReturnAddress(destination)){
    throw new FlowError(
      'invalid_redirect',
      'The authorization server returned an unsafe return address.'
    );
  }
  clearAccess();
  setStatus(message);
  location.replace(result.redirect_url);
}

async function signIn(email,password){
  const endpoint=new URL('/auth/v1/token?grant_type=password',base);
  const response=await fetch(endpoint,{
    method:'POST',
    headers:{apikey:apiKey,'Content-Type':'application/json'},
    body:JSON.stringify({email,password})
  });
  if(!response.ok){
    throw new FlowError(
      'signin_failed',
      'Sign-in failed. Check the assigned agent account and try again.'
    );
  }
  const result=await response.json();
  if(typeof result.access_token!=='string'||!result.access_token){
    throw new FlowError('signin_failed','Supabase returned no usable session.');
  }
  sessionStorage.setItem(accessKey,result.access_token);
}

async function authorization(path='',options={}){
  const access=sessionStorage.getItem(accessKey);
  if(!access)throw new FlowError('session_expired','Sign in with the assigned agent account.');
  const endpoint=new URL(
    '/auth/v1/oauth/authorizations/'+encodeURIComponent(authorizationId)+path,
    base
  );
  const headers={apikey:apiKey,'Content-Type':'application/json'};
  headers['Author'+'ization']='Bearer '+access;
  const response=await fetch(endpoint,{...options,headers});
  if(response.status===401||response.status===403){
    throw new FlowError('session_expired','The agent account session expired. Sign in again.');
  }
  if(response.status===404||response.status===410){
    throw new FlowError(
      'request_finished',
      'This authorization request expired or was already completed.'
    );
  }
  if(!response.ok){
    throw new FlowError(
      'authorization_unavailable',
      'The authorization service is unavailable. Try again.'
    );
  }
  return response.status===204?{}:response.json();
}

async function showDetails(){
  const result=await authorization();
  if(result.redirect_url){
    finish(result,'Authorization already completed. Returning to the connector…');
    return;
  }
  const scopes=normalizedScopes(result.scopes||result.scope);
  setText('client',result.client_name||result.client?.name||'Unknown client');
  setText('resource',result.resource||result.resource_uri);
  setText('scopes',scopes.join(' '));
  setText('redirect',result.redirect_uri);
  signin.hidden=true;
  details.hidden=false;
  setStatus('Review the request, then explicitly approve or deny it.');
}

async function decide(action){
  if(deciding)return;
  deciding=true;
  approve.disabled=true;
  deny.disabled=true;
  setStatus(action==='approve'?'Approving…':'Denying…');
  try{
    const result=await authorization('/consent',{method:'POST',body:JSON.stringify({action})});
    const message=action==='approve'
      ?'Approved. Returning to the connector…'
      :'Denied. Returning to the connector…';
    finish(result,message);
  }catch(error){
    deciding=false;
    approve.disabled=false;
    deny.disabled=false;
    throw error;
  }
}

form.addEventListener('submit',async event=>{
  event.preventDefault();
  const login=document.getElementById('login');
  login.disabled=true;
  setStatus('Signing in with Supabase…');
  const data=new FormData(form);
  const email=String(data.get('email')||'');
  const password=String(data.get('password')||'');
  try{
    await signIn(email,password);
    passwordInput.value='';
    await showDetails();
  }catch(error){
    passwordInput.value='';
    fail(error);
  }finally{
    login.disabled=false;
  }
});

approve.addEventListener('click',()=>decide('approve').catch(fail));
deny.addEventListener('click',()=>decide('deny').catch(fail));

(async()=>{
  if(!/^[A-Za-z0-9_-]{1,256}$/.test(authorizationId)){
    fail(new FlowError('invalid_request','Invalid authorization request.'));
    return;
  }
  if(sessionStorage.getItem(accessKey)){
    try{await showDetails();}catch(error){fail(error);}
  }
})();"""
    )


def register_consent_routes(
    server: MCPServer[object],
    settings: ConsentSettings,
) -> None:
    """Register public consent assets before creating the Streamable HTTP app."""

    if not isinstance(server, MCPServer):
        raise TypeError("server must be MCPServer")
    if not isinstance(settings, ConsentSettings):
        raise TypeError("settings must be ConsentSettings")
    headers = security_headers(settings)

    @server.custom_route(
        CONSENT_PATH,
        methods=_CONSENT_ROUTE_METHODS,
        name="supabase-oauth-consent",
        include_in_schema=False,
    )
    async def consent_page(request: Request) -> Response:
        if request.method != "GET":
            return PlainTextResponse(
                "Method not allowed.",
                status_code=405,
                headers={**headers, "Allow": "GET, HEAD"},
            )
        values = request.query_params.getlist("authorization_id")
        if len(values) != 1 or not _valid_authorization_id(values[0]):
            return PlainTextResponse(
                "Invalid or missing authorization request.",
                status_code=400,
                headers=headers,
            )
        return HTMLResponse(
            consent_html(settings, values[0]),
            headers=headers,
        )

    @server.custom_route(
        CONSENT_JAVASCRIPT_PATH,
        methods=_CONSENT_ROUTE_METHODS,
        name="supabase-oauth-consent-javascript",
        include_in_schema=False,
    )
    async def consent_script(request: Request) -> Response:
        if request.method != "GET":
            return PlainTextResponse(
                "Method not allowed.",
                status_code=405,
                headers={**headers, "Allow": "GET, HEAD"},
            )
        return Response(
            consent_javascript(),
            media_type="text/javascript",
            headers=headers,
        )

    @server.custom_route(
        CONSENT_STYLESHEET_PATH,
        methods=_CONSENT_ROUTE_METHODS,
        name="supabase-oauth-consent-stylesheet",
        include_in_schema=False,
    )
    async def consent_style(request: Request) -> Response:
        if request.method != "GET":
            return PlainTextResponse(
                "Method not allowed.",
                status_code=405,
                headers={**headers, "Allow": "GET, HEAD"},
            )
        return Response(
            consent_stylesheet(),
            media_type="text/css",
            headers=headers,
        )

    @server.custom_route(
        f"{CONSENT_PATH}/{{asset_path:path}}",
        methods=_CONSENT_ROUTE_METHODS,
        name="supabase-oauth-consent-not-found",
        include_in_schema=False,
    )
    async def consent_not_found(_request: Request) -> Response:
        return PlainTextResponse(
            "Consent asset not found.",
            status_code=404,
            headers=headers,
        )


__all__ = [
    "CONSENT_JAVASCRIPT_PATH",
    "CONSENT_PATH",
    "CONSENT_STYLESHEET_PATH",
    "STANDARD_CONSENT_SCOPES",
    "ConsentSettings",
    "consent_html",
    "consent_javascript",
    "consent_stylesheet",
    "is_allowed_consent_return_address",
    "normalized_consent_scopes",
    "register_consent_routes",
    "security_headers",
]
