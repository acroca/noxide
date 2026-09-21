# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/acroca/Noxide/security/advisories/new)
rather than opening a public issue. A first response should come within a week.

This is a personal project maintained in spare time — there is no SLA and no
bounty, but reports are genuinely welcome and will be credited unless you'd
rather not be.

## Threat model

Noxide is a single-user, self-hosted assistant holding a private journal. By
default it makes outbound connections only, plus one HTTP listener for its
web app, with **no application authentication**; see
[deployment](docs/deployment.md#4-open-the-app) before exposing it. Anyone who
can reach the listener can read conversations and use the assistant. Restrict
access through Tailscale Serve and tailnet rules (never Funnel), or equivalent
private-network controls. Keep the backend port bound to localhost. This is not
multi-tenant access control and is not suitable for public exposure.

### What it defends against

**Web browser protections.** The service validates Host and same-origin mutation
headers and does not enable CORS. These checks are not authentication: a direct
client can supply the expected headers. HTML content is escaped before the limited
Markdown renderer handles it; no raw HTML or arbitrary vault file server is
exposed. CSP blocks inline script, framing, and external dependencies. The
service worker caches only public shell assets, never API/vault responses.
Push endpoints are restricted to known browser push providers, with redirects
disabled; expired subscriptions are removed. Notifications display reply or
reminder previews (up to 500 characters), which can expose private content on
the lock screen. Control preview visibility in device notification settings.

Web drafts and uncertain-submission records are stored unencrypted in browser
local storage. Uncertain-submission records contain both a client ID and full
message text and can remain after Clear local drafts to deduplicate uncertain
sends. Clear the site's browser data to remove all local records. Messages, completed model
context, replies and push subscriptions persist in private SQLite. Protect the
device, state directory and backups accordingly. Clearing local drafts is not a
remote device wipe. Reset context retains the archive and starts a new automatic window;
nothing in the app deletes saved conversation text, and removing the
database by hand does not touch vault facts, pending queue payloads, logs or
backups. Dismissed requests keep their row so old retries cannot resurrect
reset work. SQLite secure_delete is
enabled, but this is not a guarantee of forensic erasure on the host or backups.
Push delivery is best effort, not an acknowledgment.

**Untrusted web content.** Web research runs in a `Researcher` sub-agent with a
fresh context per call and exactly two tools (search, fetch). It has no vault,
schedule, or messaging access, so injected instructions in a page have nothing
to act on and no way to reach you. Raw page content never enters the main
agent's context — only the sub-agent's summary crosses back. The only thing
that crosses outward is a ≤400-character question, which is logged.

Bulk fan-out workers follow the same pattern: each processes one item in a
fresh context and is **read-only** — vault read/list/search, skill loading,
and `research`. A worker cannot write files, schedule jobs, or message you,
and its only path to the web is the same quarantined `Researcher`, so a batch
job widens throughput without widening what a single misbehaving run can do.

**Server-side request forgery.** `fetch_page` refuses non-HTTP schemes and any
host resolving to a non-global address (private ranges, loopback, link-local
and cloud metadata endpoints). Redirects are followed manually so every hop is
re-checked, and the vetted IP is pinned for the connection — with the hostname
carried in the `Host` header and SNI — so DNS cannot be rebound between the
check and the request. Responses are capped in bytes and characters.
Page fetches request `Accept-Encoding: identity` and reject compressed responses
before reading or decoding the body, preventing decompression from bypassing
the byte limit.

**Path traversal.** Every vault file operation resolves through `_safe_path`,
which rejects anything landing outside the vault root, whether via `..` or an
absolute path. `list_files` re-checks each glob match for the same reason.
Model-supplied skill slugs are validated against `[a-z0-9-]+` before any path
join.

**Credential exposure in logs.** `httpx` request logging is turned down to
WARNING at startup, because at INFO it logs full URLs. The OAuth token is
written `0600`.

**Losing work on restart.** SIGTERM starts a drain rather than an exit: runs
in flight and a mid-run scheduled job get to finish, and anything cut short is
marked in the chat and pushed to every device — see
[Graceful restarts](docs/deployment.md#graceful-restarts).

### What it does not defend against

**Malicious attachment content.** Text extracted from a PDF, and images you
send, go into the *main* agent's context, which can write to the vault and can
call `research`. The prompt states plainly that attachment content is data and
never instructions, but a prompt is a mitigation, not a boundary. Treat
forwarding an untrusted document to the bot as you would opening it: probably
fine, occasionally not. The exfiltration channel this could reach is the
400-character research question, and every one of those is logged.

**A compromised GitHub account or device.** Whoever holds your Copilot OAuth
token can spend your Copilot quota; whoever can reach the app on your private
network is, as far as the assistant is concerned, you.

**The model's judgment.** The agent can create, rewrite, edit, append and move
files inside the vault; there is no delete tool. The file tools enforce
append-only history for `raw/journal/` and `wiki/log.md`: edits, rewrites and
moves are refused on resolved paths, while reads, appends and exclusive creates
remain allowed. This protects existing history, not the accuracy of new entries
or edits elsewhere. Keep the vault in git if you want an undo — it's plain
markdown, so this works well.

**Anyone with filesystem access to the host.** The vault is unencrypted
markdown and `state/oauth_token` is a plaintext credential at `0600`. The state
directory also holds private queued messages and the exact consumed inbox
snapshot. These are as safe as the machine they sit on.

**Denial of service and quota exhaustion.** Private network controls are
trust gates, not quotas. At most sixteen web messages are in flight at once.
Anyone who can reach the web service can still consume Copilot quota or fill
storage. Use a
private network and reverse-proxy limits rather than treating this personal
service as hardened public multi-user hosting.

## Deployment notes

- Keep `.env`, `config.toml` and `state/` out of version control (the shipped
  `.gitignore` files do this) and `chmod 600` your `.env`.
- Never commit a real vault. `/vault/` and `assistant/vault/` are gitignored;
  `vault.template/` is the only vault content in the repo and holds no data.
- The 4get service for web research publishes no ports — keep it that way, so
  it is only reachable from the Compose network.
- Pin a release tag (`ghcr.io/acroca/noxide:v1.0.0`) rather than `latest` if
  you care about knowing what you're running.
