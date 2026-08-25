# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/acroca/Noxide/security/advisories/new)
rather than opening a public issue. A first response should come within a week.

This is a personal project maintained in spare time — there is no SLA and no
bounty, but reports are genuinely welcome and will be credited unless you'd
rather not be.

## Threat model

Noxide is a single-user, self-hosted bot holding a private journal. It makes
outbound connections only and publishes no ports.

### What it defends against

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

**Path traversal.** Every vault file operation resolves through `_safe_path`,
which rejects anything landing outside the vault root, whether via `..` or an
absolute path. `list_files` re-checks each glob match for the same reason.
Model-supplied skill slugs are validated against `[a-z0-9-]+` before any path
join.

**Unauthorized users.** User-generated Telegram updates from ids outside
`allowed_user_ids` are dropped before processing, including callback queries
from inline keyboards. The configured or first-learned home chat is pinned, so
messages in another chat cannot redirect scheduled or proactive output.
Telegram-declared group migration events are accepted only when they migrate
that pinned chat.

**Credential exposure in logs.** `httpx` request logging is turned down to
WARNING at startup, because at INFO it logs full URLs — which for Telegram
includes the bot token. The OAuth token is written `0600`.

**Losing your messages on restart.** SIGTERM starts a drain rather than an
exit, because stopping the Telegram updater acknowledges every already-fetched
update. This is a data-integrity property, not just politeness — see
[Graceful restarts](docs/deployment.md#graceful-restarts).

### What it does not defend against

**Malicious attachment content.** Text extracted from a PDF, and images you
send, go into the *main* agent's context, which can write to the vault and can
call `research`. The prompt states plainly that attachment content is data and
never instructions, but a prompt is a mitigation, not a boundary. Treat
forwarding an untrusted document to the bot as you would opening it: probably
fine, occasionally not. The exfiltration channel this could reach is the
400-character research question, and every one of those is logged.

**A compromised GitHub or Telegram account.** Whoever holds your Copilot OAuth
token can spend your Copilot quota; whoever controls an allowlisted Telegram
account is, as far as the bot is concerned, you.

**The model's judgment.** The agent can write, overwrite and delete files
anywhere inside the vault. It is instructed never to edit past journal entries,
but nothing enforces that. Keep the vault in git if you want an undo — it's
plain markdown, so this works well.

**Anyone with filesystem access to the host.** The vault is unencrypted
markdown and `state/oauth_token` is a plaintext credential at `0600`. Both are
as safe as the machine they sit on.

**Denial of service and quota exhaustion.** There is no rate limiting; the
allowlist is the only gate. An allowlisted user can burn your Copilot quota.

## Deployment notes

- Keep `.env`, `config.toml` and `state/` out of version control (the shipped
  `.gitignore` files do this) and `chmod 600` your `.env`.
- Never commit a real vault. `/vault/` and `assistant/vault/` are gitignored;
  `vault.template/` is the only vault content in the repo and holds no data.
- The 4get service for web research publishes no ports — keep it that way, so
  it is only reachable from the Compose network.
- Pin a release tag (`ghcr.io/acroca/noxide:v1.0.0`) rather than `latest` if
  you care about knowing what you're running.
