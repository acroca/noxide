"""Real service-worker/offline checks against a disposable HTTP server.

mise exec uv -- uv run --with playwright python -m tests.browser_pwa_lifecycle
Requires Chromium installed through Playwright. No real vault or model calls.
"""

import asyncio
import base64
import hashlib
import json
from pathlib import Path

from aiohttp import web
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright, expect


async def main():
    assets = Path(__file__).parents[1] / "src/assistant/pwa"
    agent_name = 'Juniper'
    instance_version = hashlib.sha256(agent_name.encode()).hexdigest()[:12]
    state = {"revision": 1, "available": True, "authorized": True, "replies": [], "generation": 0, "voice": False, "unread": 0, "before": None}
    seen, uploads, submissions = [], [], []
    release = asyncio.Event()
    release.set()
    started = asyncio.Event()

    def thread_page():
        """The server's shape: threads in the order they started, each message carrying its thread."""
        threads = {}
        for message in state["replies"]:
            thread = message.get("thread") or message["id"]
            entry = threads.setdefault(thread, {"id": thread, "started": message["created"], "messages": []})
            entry["messages"].append({**message, "thread": thread})
            entry["started"] = min(entry["started"], message["created"])
        for entry in threads.values():
            entry["messages"].sort(key=lambda m: m["created"])
        return sorted(threads.values(), key=lambda t: t["started"])

    async def handle(request):
        if request.path.startswith("/api/"):
            if not state["available"]:
                return web.Response(text="Proxy: backend unavailable", status=502)
            if not state["authorized"]:
                return web.json_response({"error": "Sign in to continue"}, status=401)
            if request.path == "/api/session":
                return web.json_response({"timezone": "UTC", "push_key": "", "agent_name": agent_name, "voice": state["voice"]})
            if request.path == "/api/attachments":
                uploads.append((request.content_type, await request.read()))
                return web.json_response({"path": f"attachments/2026-09-15-{len(uploads):06x}.png"})
            if request.path == "/api/attachment":
                if state.get("thumbnail"):  # a late, tall thumbnail for the scroll-pin scenario
                    await asyncio.sleep(0.5)
                    return web.Response(body=state["thumbnail"], content_type="image/png")
                return web.Response(body=uploads[-1][1], content_type="image/png")
            if request.path == "/api/messages" and request.method == "POST":
                started.set()
                await release.wait()
                submissions.append(await request.json())
                return web.json_response({"id": submissions[-1]["id"]}, status=202)
            if request.path == "/api/seen":
                seen.append(await request.json())
                return web.json_response({"ok": True})
            if request.path == "/api/now":
                return web.json_response({"content": "# Now\n\n## Today\n- [ ] A read-only page"})
            if request.path == "/api/models":
                return web.json_response({"current": "sonnet", "default": "sonnet", "models": [
                    {"alias": "sonnet", "label": "Claude Sonnet 5", "id": "claude-sonnet-5"}]})
            if request.path != "/api/messages" or "space" in request.query:
                raise web.HTTPNotFound()  # one chat: no topic routes, no space parameter
            return web.json_response({"threads": thread_page(), "before": state["before"],
                                      "generation": state["generation"], "unread": state["unread"]})
        name = "index.html" if request.path == "/" else request.path.lstrip("/")
        if name in ("icon-192.png", "icon-512.png"):
            name = "icon.svg"
        if name not in {"index.html", "app.js", "theme.js", "sw.js", "style.css", "icon.svg", "manifest.webmanifest"}:
            raise web.HTTPNotFound()
        text = (assets / name).read_text()
        if name == 'index.html':
            text = text.replace('__AGENT_NAME__', agent_name)
        if name == "sw.js":
            text = text.replace('__INSTANCE_VERSION__', f"{instance_version}-test{state['revision']}")
            text = text.replace('"__AGENT_NAME__"', json.dumps(agent_name))
        mime = {"html": "text/html", "js": "application/javascript", "css": "text/css", "svg": "image/svg+xml", "webmanifest": "application/manifest+json"}[name.rsplit(".", 1)[-1]]
        return web.Response(text=text, content_type=mime, headers={"Cache-Control": "no-store"})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            # Record App Badging calls; headless Chromium accepts them silently.
            await context.add_init_script("""window.__badges = [];
                navigator.setAppBadge = async n => { window.__badges.push(n); };
                navigator.clearAppBadge = async () => { window.__badges.push('clear'); };""")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            url = f"http://127.0.0.1:{port}"
            await page.goto(url)
            composer = f"Message to {agent_name}"
            area = page.get_by_label(composer)
            await area.fill("Preserve this draft")
            await page.evaluate("async () => { await navigator.serviceWorker.ready; }")
            await page.wait_for_function("() => !!navigator.serviceWorker.controller")
            await expect(page.locator("#update-banner")).to_be_hidden()

            # On the phone the navigation lives in the header, so the composer
            # is the bottom edge: it carries the home-indicator inset, dropped
            # while the field has focus and the keyboard sits below the shell.
            await expect(page.locator('.topbar-nav a.active')).to_have_text('Chat')
            assert await page.evaluate("() => document.querySelector('.topbar-nav').getBoundingClientRect().top < 54")
            await page.add_style_tag(content=":root{--navigation-safe-area:34px}")
            await area.blur()
            await expect(page.locator('.chat-status')).to_have_css('padding-bottom', '43px')
            await area.focus()
            await expect(page.locator('.chat-status')).to_have_css('padding-bottom', '9px')
            await area.blur()
            await expect(page.locator('.chat-status')).to_have_css('padding-bottom', '43px')
            # The shell follows a shrunken visual viewport (a software keyboard).
            await page.evaluate("() => document.documentElement.style.setProperty('--shell-height', '500px')")
            await expect(page.locator('.shell')).to_have_css('height', '500px')
            # ...and is recomputed when the app comes back to the foreground, since a
            # keyboard dismissed in the background fires no viewport event.
            await page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
            await expect(page.locator('.shell')).to_have_css('height', '844px')
            # iOS sometimes leaves the window itself shrunk once the keyboard has
            # gone (WebKit standalone-PWA bug): every height reads short and no
            # event fires. The cure is sending the shell through a layout a
            # moment after the field blurs and after the app returns; it must
            # keep the thread where it was scrolled and never run while the
            # field has focus, since hiding the shell would close the keyboard.
            await page.evaluate("""() => {
              const thread = document.querySelector('#chat-thread');
              const filler = document.createElement('div'); filler.style.height = '5000px'; thread.append(filler);
              thread.scrollTop = 1200; thread.dispatchEvent(new Event('scroll'));
              window.__relayouts = 0;
              new MutationObserver(records => { window.__relayouts += records.filter(r => /display: none/.test(r.oldValue || '')).length; })
                .observe(document.querySelector('.shell'), { attributes: true, attributeFilter: ['style'], attributeOldValue: true });
            }""")
            await area.focus()
            await area.blur()
            await page.wait_for_function("() => window.__relayouts > 0")
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollTop") == 1200
            await expect(page.locator('.shell')).to_have_css('display', 'flex')
            await expect(page.locator('.shell')).to_have_css('height', '844px')
            await area.focus()
            await page.evaluate("() => { window.__relayouts = 0; document.dispatchEvent(new Event('visibilitychange')); }")
            await page.wait_for_timeout(700)
            assert await page.evaluate("() => window.__relayouts") == 0
            await expect(area).to_be_focused()
            await area.blur()
            await page.evaluate("() => document.querySelector('#chat-thread').lastChild.remove()")
            # One chat: no topic picker, no channel links, and no second header
            # row under the topbar; Reset context lives in Preferences.
            assert await page.locator('select:not(#model-select)').count() == 0
            assert await page.locator('#topic-links, #topic-picker, #chat-topic, .channel-picker').count() == 0
            assert await page.locator('.chat-header, .chat-title').count() == 0
            await expect(page.locator('#breadcrumb')).to_have_text('Chat')
            assert await page.title() == f'{agent_name} · Chat'
            await expect(page.get_by_role('button', name='Reset context')).to_be_hidden()
            assert await page.locator('#settings #reset-context').count() == 1
            # The draft survives a trip to Now and back, and any chat sub-path opens the chat.
            await page.goto(url + '/#now')
            await expect(page.locator('#now-content')).to_be_visible()
            await expect(page.locator('#breadcrumb')).to_have_text('Now')
            await page.goto(url + '/#chat/anything')
            await expect(area).to_have_value('Preserve this draft')
            await page.goto(url + '/#chat')
            await expect(area).to_have_value('Preserve this draft')

            # No sign-in is needed; offline launches keep drafts.
            assert await page.locator('input[type="password"]').count() == 0
            await context.set_offline(True)
            await page.reload()
            await expect(page.get_by_role("heading", name=f"{agent_name} is unavailable")).to_be_visible()
            assert await page.evaluate("localStorage.getItem('noxide-draft:general')") == "Preserve this draft"
            await context.set_offline(False)
            # Regaining connectivity boots on its own (the online event); the
            # button is for when it does not, and may already be gone.
            try:
                await page.get_by_role("button", name="Try again").click(timeout=2000)
            except PlaywrightTimeoutError:
                pass
            await expect(area).to_have_value("Preserve this draft")

            # Proxy failures, including access denial, remain recoverable.
            state["available"] = False
            await page.reload()
            await expect(page.locator("#unavailable")).to_be_visible()
            state["available"] = True
            state["authorized"] = False
            await page.get_by_role("button", name="Try again").click()
            await expect(page.locator("#unavailable")).to_be_visible()
            state["authorized"] = True
            await page.reload()
            await expect(area).to_have_value("Preserve this draft")

            # Two clients keep their own open drafts. Updating one must not
            # force a reload in the other while the user is typing there.
            other = await context.new_page()
            await other.goto(url + "/#chat")
            await other.get_by_label(composer).fill("Other tab draft")
            await area.fill("This tab draft")
            state["revision"] = 2
            await page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
            await expect(page.locator("#update-banner")).to_be_visible()
            await expect(other.locator("#update-banner")).to_be_visible()
            await expect(area).to_have_value("This tab draft")

            # Reload is unavailable during an uncertain submission.
            release.clear()
            await page.get_by_role("button", name="Send message", exact=True).click()
            await asyncio.wait_for(started.wait(), timeout=5)
            await expect(page.locator("#reload-update")).to_be_disabled()
            release.set()
            await expect(page.locator("#reload-update")).to_be_enabled()
            await expect(area).to_have_value("")
            await area.fill("Saved before update")
            await page.locator("#reload-update").click()
            await expect(page.locator("#update-banner")).to_be_hidden()
            await expect(area).to_have_value("Saved before update")
            await expect(other.get_by_label(composer)).to_have_value("Other tab draft")
            page.on("dialog", lambda dialog: dialog.accept())
            await page.get_by_role("button", name="Preferences", exact=True).click()
            await page.get_by_role("button", name="Clear local drafts", exact=True).click()
            await expect(area).to_have_value("")
            assert await page.evaluate("localStorage.getItem('noxide-draft:general')") is None
            await expect(other.locator("#update-banner")).to_be_visible()
            await other.locator("#reload-update").click()
            await expect(other.locator("#update-banner")).to_be_hidden()
            await expect(other.get_by_label(composer)).to_have_value("Other tab draft")
            assert await page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")

            # A focused device showing the newest reply acknowledges it once;
            # a device on Now, or a hidden one, acknowledges nothing.
            assert seen == [], seen
            await other.close()
            reply = {"id": "r1", "space": "general", "role": "assistant", "text": "Done.", "status": "done",
                     "created": 1700000000.5, "source": "web", "delivery": "available", "generation": 0}
            state["replies"] = [reply]
            await page.bring_to_front()
            await expect(page.locator(".message-assistant")).to_be_visible()
            await page.wait_for_function("() => document.querySelector('#chat-thread') && document.hasFocus()")
            await asyncio.sleep(3)
            assert seen == [{"through": 1700000000.5}], seen
            # Same device on the Now page: the chat's newer reply is not acknowledged.
            await page.goto(url + "/#now")
            await expect(page.locator("#now-content")).to_be_visible()
            state["replies"] = [reply, {**reply, "id": "r2", "created": 1700000001.5}]
            await asyncio.sleep(3)
            assert seen == [{"through": 1700000000.5}], seen
            # Back on the chat but unfocused (headless tabs cannot lose focus for real).
            await page.goto(url + "/#chat")
            await page.evaluate("() => { document.hasFocus = () => false; }")
            await asyncio.sleep(3)
            assert seen == [{"through": 1700000000.5}], seen
            # Focused but idle: a window left open with no input for a few
            # minutes acknowledges nothing until someone touches it again.
            await page.evaluate("() => { delete document.hasFocus; }")
            await page.clock.install()
            await page.clock.fast_forward(4 * 60 * 1000)
            await page.clock.run_for(3000)
            await asyncio.sleep(1)  # the poll's requests complete in real time
            assert seen == [{"through": 1700000000.5}], seen
            await page.mouse.move(40, 40)
            await page.clock.run_for(3000)
            await asyncio.sleep(1)
            assert seen == [{"through": 1700000000.5}, {"through": 1700000001.5}], seen
            await page.clock.resume()
            # A notification click: the worker asks for the chat and the page
            # switches its own hash, keeping the draft it left on the composer.
            await page.evaluate("() => document.querySelector('#settings').close()")  # modal would trap focus
            await page.get_by_label(composer).fill("half-written")
            await page.goto(url + "/#now")
            await expect(page.locator("#now-content")).to_be_visible()
            await page.evaluate("() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {data: {type: 'OPEN_CHAT'}}))")
            await expect(page.get_by_label(composer)).to_have_value("half-written")
            assert await page.evaluate("() => location.hash") == "#chat"
            # Already on the chat, the same message re-renders it without a hash change.
            await page.evaluate("() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {data: {type: 'OPEN_CHAT'}}))")
            await expect(page.get_by_label(composer)).to_have_value("half-written")
            assert await page.evaluate("() => location.hash") == "#chat"
            await page.get_by_label(composer).fill("")

            # A message still being answered does not block the next one: it
            # queues behind it on the server, so Send stays enabled.
            state["replies"].append({"id": "u9", "space": "general", "role": "user", "text": "First", "status": "queued",
                                     "created": 1700000004.5, "source": "web", "generation": 1})
            await expect(page.locator(".message-status")).to_have_text("Queued…")
            state["replies"][-1]["activity"] = "Searching the web…"
            await expect(page.locator(".message-status")).to_have_text("Searching the web…")
            await expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
            await expect(page.locator("#chat-status")).to_contain_text("Ready")
            state["replies"].pop()
            await expect(page.locator(".message-status")).to_have_count(0)

            # The Home Screen badge follows the server's unread count.
            state["unread"] = 2
            await page.wait_for_function("() => window.__badges.at(-1) === 2")
            state["unread"] = 0
            await page.wait_for_function("() => window.__badges.at(-1) === 'clear'")

            # The composer starts one line tall and grows with the text.
            height = "() => document.querySelector('#chat-form textarea').offsetHeight"
            one_line = await page.evaluate(height)
            await page.get_by_label(composer).fill("one\ntwo\nthree\nfour")
            assert await page.evaluate(height) > one_line
            await page.get_by_label(composer).fill("")
            assert await page.evaluate(height) == one_line

            # Appearance: an explicit choice overrides the device scheme and
            # survives a reload without flashing; System clears the override
            # and a dark device gets the dark palette with nothing stored.
            background = "() => getComputedStyle(document.body).backgroundColor"
            theme_attr = "() => document.documentElement.dataset.theme ?? null"
            assert await page.evaluate(theme_attr) is None
            light = await page.evaluate(background)
            await page.get_by_role("button", name="Preferences", exact=True).click()
            await expect(page.get_by_label("System", exact=True)).to_be_checked()
            await page.get_by_label("Dark", exact=True).check()
            assert await page.evaluate(theme_attr) == "dark"
            dark = await page.evaluate(background)
            assert dark != light, (dark, light)
            await page.reload()
            await expect(page.get_by_label(composer)).to_be_visible()
            assert await page.evaluate(theme_attr) == "dark"
            assert await page.evaluate(background) == dark
            assert await page.evaluate("() => document.querySelector('meta[name=theme-color]').content") != "#eeeee7"
            await page.get_by_role("button", name="Preferences", exact=True).click()
            await expect(page.get_by_label("Dark", exact=True)).to_be_checked()
            await page.get_by_label("System", exact=True).check()
            assert await page.evaluate(theme_attr) is None
            assert await page.evaluate(background) == light
            await page.evaluate("() => document.querySelector('#settings').close()")
            night = await browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
            night_page = await night.new_page()
            await night_page.goto(url + "/#chat")
            await expect(night_page.get_by_label(composer)).to_be_visible()
            assert await night_page.evaluate(theme_attr) is None
            assert await night_page.evaluate(background) == dark
            await night.close()

            # Reset context draws a divider: after the last message when
            # nothing has followed yet, then between generations.
            assert await page.locator(".context-divider").count() == 0
            assert await page.get_by_role("button", name="Delete chat").count() == 0
            state["generation"] = 1
            await expect(page.locator(".context-divider")).to_have_count(1)
            assert await page.evaluate("() => document.querySelector('#chat-thread').lastElementChild.className") == "context-divider"
            state["replies"].append({**reply, "id": "r3", "created": 1700000002.5, "generation": 1})
            await expect(page.locator(".message-assistant")).to_have_count(3)
            await expect(page.locator(".context-divider")).to_have_count(1)
            assert await page.evaluate("() => document.querySelector('.context-divider').nextElementSibling.textContent.includes('Done.')")
            # The divider sits between threads, and the timeline ends on the newest thread, not a divider.
            assert await page.evaluate("() => document.querySelector('.context-divider').nextElementSibling.className") == "thread"
            assert await page.evaluate("() => document.querySelector('#chat-thread').lastElementChild.className") == "thread"
            # No name labels or full date lines: the side says who wrote it and
            # the time sits small inside the bubble.
            assert await page.locator(".message-meta").count() == 0
            state["replies"].append({**reply, "id": "r4", "created": 1700000003.5, "generation": 1, "reply_to": "u1"})
            await expect(page.locator('[data-message="r4"] .message-time')).to_have_count(1)
            assert await page.evaluate("() => /^\\d{1,2}:\\d{2}( [AP]M)?$/.test(document.querySelector('[data-message=\"r4\"] .message-time').textContent)")
            # Every day gets its own separator line, like a chat app.
            await expect(page.locator(".day-divider")).to_have_count(1)
            assert await page.evaluate("() => document.querySelector('#chat-thread').firstElementChild.className") == "day-divider"
            # Load earlier messages sits at the top of the scrolling timeline,
            # only when there is an older page, so it is reached by scrolling up.
            await expect(page.locator("#older-messages")).to_have_count(0)
            state["before"] = "older-cursor"
            await expect(page.locator("#older-messages")).to_have_count(1)
            assert await page.evaluate("() => document.querySelector('#chat-thread').firstElementChild.id") == "older-messages"
            state["before"] = None
            await expect(page.locator("#older-messages")).to_have_count(0)
            # Blurring the composer restores its inset and shrinks the thread;
            # the thread stays anchored to its end, unless the reader scrolled up.
            # Chromium re-anchors a shrinking scroller by itself, so this cannot
            # fail here without the fix; WebKit (iOS) leaves the offset and ends
            # up an inset above the end, which is what the ResizeObserver fixes.
            await page.add_style_tag(content=":root{--navigation-safe-area:34px}")  # lost on the reloads above
            await page.evaluate("() => document.querySelector('#settings').close()")  # modal would trap focus
            state["replies"].extend({**reply, "id": f"r{i}", "created": 1700000010 + i, "generation": 1, "text": "Filler line " * 12} for i in range(10, 40))
            await expect(page.locator(".message-assistant")).to_have_count(34)
            gap = "() => { const t = document.querySelector('#chat-thread'); return t.scrollHeight - t.scrollTop - t.clientHeight; }"

            async def scroll_thread(top):
                """Scroll the thread as a reader would, and let the scroll event land: it carries the intent."""
                await page.evaluate(f"() => {{ document.querySelector('#chat-thread').scrollTop = {top}; }}")
                await page.evaluate("() => new Promise(r => requestAnimationFrame(() => setTimeout(r)))")
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollHeight > document.querySelector('#chat-thread').clientHeight * 2")
            await scroll_thread("document.querySelector('#chat-thread').scrollHeight")
            await page.get_by_label(composer).focus()
            await page.wait_for_function("() => document.querySelector('#chat-thread').scrollHeight - document.querySelector('#chat-thread').scrollTop - document.querySelector('#chat-thread').clientHeight < 1")
            await page.get_by_label(composer).blur()
            await page.wait_for_function(f"() => ({gap})() < 1")
            await scroll_thread(0)
            await page.get_by_label(composer).focus()
            await page.get_by_label(composer).blur()
            await asyncio.sleep(0.5)
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollTop") == 0
            # Growing the composer by several lines moves neither an end-anchored
            # thread nor one the reader scrolled up in.
            await page.get_by_label(composer).fill("")
            await scroll_thread("document.querySelector('#chat-thread').scrollHeight")
            await page.get_by_label(composer).fill("one\ntwo\nthree\nfour\nfive")
            assert await page.evaluate(f"() => ({gap})()") < 1
            await page.get_by_label(composer).fill("")
            await scroll_thread(120)
            await page.get_by_label(composer).fill("one\ntwo\nthree\nfour\nfive")
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollTop") == 120
            await page.get_by_label(composer).fill("")
            # Images: a pasted screenshot becomes a pending thumbnail, is
            # uploaded on send, and the stored path rides on the message.
            assert await page.locator("#record-voice").is_hidden()  # no transcriber configured
            paste = """async () => {
                const canvas = document.createElement('canvas'); canvas.width = 3000; canvas.height = 10;
                canvas.getContext('2d').fillStyle = '#c33'; canvas.getContext('2d').fillRect(0, 0, 3000, 10);
                const blob = await new Promise(r => canvas.toBlob(r, 'image/png'));
                const transfer = new DataTransfer();
                transfer.items.add(new File([blob], 'shot.png', {type: 'image/png'}));
                document.querySelector('#chat-form textarea').dispatchEvent(new ClipboardEvent('paste', {clipboardData: transfer, bubbles: true}));
            }"""
            await page.evaluate(paste)
            await expect(page.locator("#composer-images img")).to_have_count(1)
            await page.evaluate(paste)
            await expect(page.locator("#composer-images img")).to_have_count(2)
            await page.get_by_role("button", name="Remove attachment 2").click()
            await expect(page.locator("#composer-images img")).to_have_count(1)
            await area.fill("What is this?")
            await page.get_by_role("button", name="Send message", exact=True).click()
            await expect(page.locator("#composer-images")).to_be_hidden()
            await expect(area).to_have_value("")
            assert len(uploads) == 1 and uploads[0][0] == "image/jpeg" and uploads[0][1][:3] == b"\xff\xd8\xff", uploads[0][0]
            assert submissions[-1]["text"] == "What is this?" and submissions[-1]["attachments"] == ["attachments/2026-09-15-000001.png"]
            assert all("space" not in s for s in submissions), submissions
            state["replies"].append({"id": "u9", "space": "general", "role": "user", "text": "What is this?", "status": "done",
                                     "created": 1700000100, "source": "web", "generation": 1,
                                     "metadata": json.dumps({"attachments": ["attachments/2026-09-15-000001.png"]})})
            await expect(page.locator(".message-user .message-images img")).to_have_count(1)
            assert await page.locator(".message-user .message-images a").get_attribute("href") == "/api/attachment?path=attachments%2F2026-09-15-000001.png"
            # An end-anchored thread stays at the end while the timeline grows
            # under it: a thumbnail has no reserved height, so it lands after
            # the render pinned the end and pushes the end away. Pinning by
            # measuring the gap then judged the reader scrolled up and left
            # every later message unpinned (2026-09-19). Only the reader's own
            # scroll releases the anchor.
            state["thumbnail"] = base64.b64decode(await page.evaluate("""() => {
                const c = document.createElement('canvas'); c.width = 200; c.height = 200;
                c.getContext('2d').fillStyle = '#c33'; c.getContext('2d').fillRect(0, 0, 200, 200);
                return c.toDataURL('image/png').split(',')[1]; }"""))
            await scroll_thread("document.querySelector('#chat-thread').scrollHeight")
            await page.wait_for_function(f"() => ({gap})() < 1")
            state["replies"].append({**reply, "id": "p1", "role": "user", "text": "Look at this", "created": 1700000110,
                                     "generation": 1, "metadata": json.dumps({"attachments": ["attachments/2026-09-15-late.png"]})})
            await page.wait_for_function("() => (document.querySelector('[data-message=\"p1\"] img')?.naturalHeight || 0) > 0")
            assert await page.evaluate("() => document.querySelector('[data-message=\"p1\"] img').getBoundingClientRect().height") > 100
            await page.wait_for_function(f"() => ({gap})() < 1", timeout=3000)
            state["replies"].append({**reply, "id": "p2", "text": "A red square.", "created": 1700000111, "generation": 1, "thread": "p1"})
            await expect(page.locator('[data-message="p2"]')).to_have_count(1)
            await page.wait_for_function(f"() => ({gap})() < 1", timeout=3000)
            # ...unless the reader scrolled up, in which case nothing moves them.
            await scroll_thread(0)
            state["replies"].append({**reply, "id": "p3", "text": "Still a red square.", "created": 1700000112, "generation": 1, "thread": "p1"})
            await expect(page.locator('[data-message="p3"]')).to_have_count(1)
            await asyncio.sleep(0.5)
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollTop") == 0
            del state["thumbnail"]
            state["voice"] = True
            await page.reload()
            await expect(page.get_by_role("button", name="Record voice message")).to_be_visible()

            # Threads: a root and its replies render as one box, in the order the
            # threads started, each ending on a Reply link; links inside keep working.
            def message(id, role, text, created, thread=None, reply_to=None):
                return {"id": id, "space": "general", "role": role, "text": text, "status": "done", "created": created,
                        "source": "web", "generation": 1, "thread": thread or id, "reply_to": reply_to}
            state["replies"] = [
                message("u1", "user", "Water the plants", 1700000200),
                message("reply:u1", "assistant", "Done.", 1700000201, "u1", "u1"),
                message("u2", "user", "Plan the walk", 1700000210),
                message("reply:u2", "assistant", "Which day?", 1700000211, "u2", "u2"),
                message("u3", "user", "Saturday", 1700000212, "u2", "reply:u2"),
                message("reply:u3", "assistant", "Saturday it is.", 1700000213, "u2", "u3"),
                message("u4", "user", "A note to myself, still unanswered", 1700000220),
            ]
            await expect(page.locator("section.thread")).to_have_count(3)
            assert await page.evaluate("() => [...document.querySelectorAll('section.thread')].map(s => s.dataset.thread)") == ["u1", "u2", "u4"]
            assert await page.evaluate("() => [...document.querySelectorAll('section.thread')].map(s => [...s.querySelectorAll('article')].map(a => a.dataset.message))") == [["u1", "reply:u1"], ["u2", "reply:u2", "u3", "reply:u3"], ["u4"]]
            assert await page.evaluate("() => [...document.querySelectorAll('section.thread')].map(s => s.querySelector('.thread-reply')?.dataset.reply)") == ["u1", "u2", "u4"]
            assert await page.evaluate("() => [...document.querySelectorAll('section.thread')].every(s => !s.getAttribute('role') && s.lastElementChild.classList.contains('thread-reply'))")
            # The assistant's messages sit left, yours right, inside the box.
            assert await page.evaluate("() => { const box = document.querySelector('section.thread[data-thread=\"u1\"]').getBoundingClientRect(); const u = document.querySelector('[data-message=\"u1\"]').getBoundingClientRect(); const a = document.querySelector('[data-message=\"reply:u1\"]').getBoundingClientRect(); return a.left - box.left < 40 && box.right - u.right < 40 && a.left < u.left; }")
            assert await page.locator(".context-divider").count() == 0
            # Reply: the link replies to the thread's last message; the box is
            # marked and its link reads Replying, the chip names who is being
            # answered, the status line says so, and the send carries that message.
            await expect(page.locator("#reply-chip")).to_be_hidden()
            await page.locator('section.thread[data-thread="u2"] .thread-reply').click()
            await expect(page.locator("#reply-chip")).to_be_visible()
            assert await page.evaluate("() => [...document.querySelectorAll('section.thread')].map(s => s.classList.contains('replying'))") == [False, True, False]
            await expect(page.locator('section.thread[data-thread="u2"] .thread-reply')).to_have_text("Replying")
            await expect(page.locator('section.thread[data-thread="u1"] .thread-reply')).to_have_text("Reply")
            await expect(page.locator("#reply-excerpt")).to_have_text(f"Replying to {agent_name}: Saturday it is.")
            await expect(page.locator("#chat-status")).to_contain_text("Replying in a thread")
            await expect(area).to_be_focused()
            await area.fill("Leaving at eight")
            await page.get_by_role("button", name="Send message", exact=True).click()
            await expect(area).to_have_value("")
            assert submissions[-1]["text"] == "Leaving at eight" and submissions[-1]["reply_to"] == "reply:u3", submissions[-1]
            await expect(page.locator("#reply-chip")).to_be_hidden()
            await expect(page.locator("#chat-status")).to_contain_text("Ready")
            assert await page.evaluate("() => document.querySelectorAll('section.thread.replying').length") == 0
            await expect(page.locator('section.thread[data-thread="u2"] .thread-reply')).to_have_text("Reply")
            # A thread whose last message is the user's own reads "yourself"; the
            # active link again, or Cancel, clears the chip, and the next send
            # starts a new thread. A tap on the box itself does nothing.
            await page.locator('section.thread[data-thread="u4"] .thread-reply').click()
            await expect(page.locator("#reply-excerpt")).to_have_text("Replying to yourself: A note to myself, still unanswered")
            await page.locator('section.thread[data-thread="u4"] .thread-reply').click()
            await expect(page.locator("#reply-chip")).to_be_hidden()
            await page.locator('section.thread[data-thread="u4"] .message-body').click()
            await expect(page.locator("#reply-chip")).to_be_hidden()
            # Swiping a box to the right with a finger replies too; a short or a
            # vertical drag does not.
            swipe = """([thread, dx, dy]) => { const s = document.querySelector(`section.thread[data-thread="${thread}"]`); const r = s.getBoundingClientRect();
                const ev = (type, x, y) => s.dispatchEvent(new PointerEvent(type, {bubbles: true, pointerId: 7, pointerType: 'touch', isPrimary: true, clientX: x, clientY: y}));
                ev('pointerdown', r.left + 20, r.top + 20); ev('pointermove', r.left + 20 + dx / 2, r.top + 20 + dy / 2); ev('pointermove', r.left + 20 + dx, r.top + 20 + dy);
                const mid = s.style.transform; ev('pointerup', r.left + 20 + dx, r.top + 20 + dy); return mid; }"""
            assert await page.evaluate(swipe, ["u4", 70, 0]) == "translateX(70px)"
            await expect(page.locator("#reply-chip")).to_be_visible()
            await expect(page.locator("#reply-excerpt")).to_have_text("Replying to yourself: A note to myself, still unanswered")
            assert await page.evaluate("() => document.querySelector('section.thread[data-thread=\"u4\"]').style.transform") == ""
            assert await page.evaluate(swipe, ["u4", 30, 0]) == "translateX(30px)"
            await expect(page.locator("#reply-chip")).to_be_visible()
            assert await page.evaluate(swipe, ["u4", 30, 70]) == ""
            await expect(page.locator("#reply-chip")).to_be_visible()
            assert await page.evaluate(swipe, ["u4", 70, 0]) == "translateX(70px)"
            await expect(page.locator("#reply-chip")).to_be_hidden()
            await page.locator('section.thread[data-thread="u4"] .thread-reply').click()
            await expect(page.locator("#reply-chip")).to_be_visible()
            await page.get_by_role("button", name="Cancel reply", exact=True).click()
            await expect(page.locator("#reply-chip")).to_be_hidden()
            await expect(page.locator("#chat-status")).to_contain_text("Ready")
            await area.fill("Something new")
            await page.get_by_role("button", name="Send message", exact=True).click()
            await expect(area).to_have_value("")
            assert submissions[-1]["text"] == "Something new" and submissions[-1]["reply_to"] is None, submissions[-1]
            # Long excerpts are clipped so the chip stays one line.
            state["replies"].append(message("u5", "user", "word " * 40, 1700000230))
            await expect(page.locator("section.thread")).to_have_count(4)
            await page.locator('section.thread[data-thread="u5"] .thread-reply').click()
            await expect(page.locator("#reply-excerpt")).to_have_text("Replying to yourself: " + ("word " * 18).rstrip() + "…")
            await page.get_by_role("button", name="Cancel reply", exact=True).click()
            # Threads from different days are separated by a day line: Today,
            # Yesterday, then the date.
            state["replies"].append(message("old", "user", "From another day", 1600000000))
            await expect(page.locator(".day-divider")).to_have_count(2)
            await expect(page.locator(".day-divider").first).to_have_text("September 13, 2020")
            # #chat/<thread> (a notification with no open window) opens that thread
            # in reply mode and drops the thread from the URL again.
            await page.goto(url + "/#chat/u1")
            await expect(page.locator("#reply-chip")).to_be_visible()
            await expect(page.locator("#reply-excerpt")).to_have_text(f"Replying to {agent_name}: Done.")
            assert await page.evaluate("() => location.hash") == "#chat"
            await expect(area).to_be_focused()
            # A thread that is not on the page opens the chat with no reply mode.
            await page.goto(url + "/#chat/nothing-here")
            await expect(page.get_by_label(composer)).to_be_visible()
            await expect(page.locator("#reply-chip")).to_be_hidden()
            assert await page.evaluate("() => location.hash") == "#chat"
            await page.goto(url + "/#chat/u2")
            await expect(page.locator("#reply-excerpt")).to_have_text(f"Replying to {agent_name}: Saturday it is.")
            assert await page.evaluate("() => location.hash") == "#chat"
            # A notification click with the app open: the worker names the thread,
            # from the Now page or from the chat itself.
            await page.goto(url + "/#now")
            await expect(page.locator("#now-content")).to_be_visible()
            await page.evaluate("() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {data: {type: 'OPEN_CHAT', thread: 'u1'}}))")
            await expect(page.locator("#reply-excerpt")).to_have_text(f"Replying to {agent_name}: Done.")
            assert await page.evaluate("() => location.hash") == "#chat"
            await page.evaluate("() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {data: {type: 'OPEN_CHAT', thread: 'u2'}}))")
            await expect(page.locator("#reply-excerpt")).to_have_text(f"Replying to {agent_name}: Saturday it is.")
            assert await page.evaluate("() => location.hash") == "#chat"
            # A click without a thread opens the chat with no reply mode.
            await page.evaluate("() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {data: {type: 'OPEN_CHAT', thread: null}}))")
            await expect(page.locator("#reply-chip")).to_be_hidden()
            await expect(page.locator("#chat-status")).to_contain_text("Ready")

            keys = await page.evaluate("() => caches.keys()")
            assert keys == [f"noxide-shell-{instance_version}-test2"], keys
            assert not errors, errors
            await browser.close()
            print("Passed: password-free startup, single chat without topics, offline/proxy failure recovery, waiting update, mutation guard, draft-safe multi-tab reload, local draft clearing, mobile overflow, seen acknowledgements, notification click to chat, reset dividers, pasted images, end-pinned timeline across late thumbnails, voice button, thread sections, reply chip and reply_to, cancel reply, #chat/<thread> and OPEN_CHAT reply mode.")
    finally:
        release.set()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
