"""Real service-worker/offline checks against a disposable HTTP server.

mise exec uv -- uv run --with playwright python -m tests.browser_pwa_lifecycle
Requires Chromium installed through Playwright. No real vault or model calls.
"""

import asyncio
import hashlib
import json
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright, expect


async def main():
    assets = Path(__file__).parents[1] / "src/assistant/pwa"
    agent_name = 'Juniper'
    instance_version = hashlib.sha256(agent_name.encode()).hexdigest()[:12]
    state = {"revision": 1, "available": True, "authorized": True, "replies": {}, "generation": 0, "voice": False, "unread": 0}
    seen, uploads, submissions = [], [], []
    release = asyncio.Event()
    release.set()
    started = asyncio.Event()

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
                return web.Response(body=uploads[-1][1], content_type="image/png")
            if request.path == "/api/topics":
                return web.json_response({"topics": [{"id": "general", "name": "General"}, {"id": "topic:10", "name": "Work"}]})
            if request.path == "/api/messages" and request.method == "POST":
                started.set()
                await release.wait()
                submissions.append(await request.json())
                return web.json_response({"id": submissions[-1]["id"]}, status=202)
            if request.path == "/api/seen":
                seen.append(await request.json())
                return web.json_response({"ok": True})
            space = request.query.get("space", "general")
            return web.json_response({"messages": state["replies"].get(space, []), "before": None,
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
            area = page.get_by_label("Message to General")
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
            assert await page.locator('select').count() == 0
            await page.get_by_role('button', name='Change topic: General').click()
            await expect(page.get_by_role('dialog', name='Choose a topic')).to_be_visible()
            await page.locator('#topic-options').get_by_role('link', name='Work', exact=True).click()
            await expect(page.get_by_label('Message to Work')).to_be_visible()
            await expect(page.locator('#topic-picker')).not_to_be_visible()
            await page.get_by_role('button', name='Change topic: Work').click()
            await page.keyboard.press('Escape')
            await expect(page.get_by_role('button', name='Change topic: Work')).to_be_focused()
            await page.get_by_role('button', name='Change topic: Work').click()
            await page.locator('#topic-options').get_by_role('link', name='General', exact=True).click()
            await expect(area).to_have_value('Preserve this draft')

            # No sign-in is needed; offline launches keep drafts.
            assert await page.locator('input[type="password"]').count() == 0
            await context.set_offline(True)
            await page.reload()
            await expect(page.get_by_role("heading", name=f"{agent_name} is unavailable")).to_be_visible()
            assert await page.evaluate("localStorage.getItem('noxide-draft:general')") == "Preserve this draft"
            await context.set_offline(False)
            await page.get_by_role("button", name="Try again").click()
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
            await other.get_by_label("Message to General").fill("Other tab draft")
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
            await expect(other.get_by_label("Message to General")).to_have_value("Other tab draft")
            page.on("dialog", lambda dialog: dialog.accept())
            await page.get_by_role("button", name="Preferences", exact=True).click()
            await page.get_by_role("button", name="Clear local drafts", exact=True).click()
            await expect(area).to_have_value("")
            assert await page.evaluate("localStorage.getItem('noxide-draft:general')") is None
            await expect(other.locator("#update-banner")).to_be_visible()
            await other.locator("#reload-update").click()
            await expect(other.locator("#update-banner")).to_be_hidden()
            await expect(other.get_by_label("Message to General")).to_have_value("Other tab draft")
            assert await page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")

            # A focused device showing the newest reply acknowledges it once;
            # a device on another topic, or a hidden one, acknowledges nothing.
            assert seen == [], seen
            await other.close()
            reply = {"id": "r1", "space": "general", "role": "assistant", "text": "Done.", "status": "done",
                     "created": 1700000000.5, "source": "web", "delivery": "available", "generation": 0}
            state["replies"]["general"] = [reply]
            await page.bring_to_front()
            await expect(page.locator(".message-assistant")).to_be_visible()
            await page.wait_for_function("() => document.querySelector('#chat-thread') && document.hasFocus()")
            await asyncio.sleep(3)
            assert seen == [{"space": "general", "through": 1700000000.5}], seen
            # Same device, other topic: General's newer reply is not acknowledged.
            await page.goto(url + "/#chat/topic%3A10")
            await expect(page.get_by_label("Message to Work")).to_be_visible()
            state["replies"]["general"] = [reply, {**reply, "id": "r2", "created": 1700000001.5}]
            await asyncio.sleep(3)
            assert seen == [{"space": "general", "through": 1700000000.5}], seen
            # Back on General but unfocused (headless tabs cannot lose focus for real).
            await page.goto(url + "/#chat")
            await page.evaluate("() => { document.hasFocus = () => false; }")
            await asyncio.sleep(3)
            assert seen == [{"space": "general", "through": 1700000000.5}], seen
            await page.evaluate("() => { delete document.hasFocus; window.dispatchEvent(new Event('focus')); }")
            await asyncio.sleep(1)
            assert seen == [{"space": "general", "through": 1700000000.5}, {"space": "general", "through": 1700000001.5}], seen
            # A notification click: the worker names the channel and the page
            # switches its own hash, keeping the General draft it never left.
            await page.evaluate("() => document.querySelector('#settings').close()")  # modal would trap focus
            await page.get_by_label("Message to General").fill("half-written")
            await page.evaluate("() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {data: {type: 'OPEN_SPACE', space: 'topic:10'}}))")
            await expect(page.get_by_label("Message to Work")).to_be_visible()
            assert await page.evaluate("() => location.hash") == "#chat/topic%3A10"
            await page.evaluate("() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {data: {type: 'OPEN_SPACE', space: 'general'}}))")
            await expect(page.get_by_label("Message to General")).to_have_value("half-written")
            await page.get_by_label("Message to General").fill("")

            # A message still being answered does not block the next one: it
            # queues behind it on the server, so Send stays enabled.
            state["replies"]["general"].append({"id": "u9", "space": "general", "role": "user", "text": "First", "status": "queued",
                                                "created": 1700000004.5, "source": "web", "generation": 1})
            await expect(page.locator(".message-status")).to_have_text("Queued…")
            state["replies"]["general"][-1]["activity"] = "Searching the web…"
            await expect(page.locator(".message-status")).to_have_text("Searching the web…")
            await expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
            await expect(page.locator("#chat-status")).to_contain_text("Writing in General")
            state["replies"]["general"].pop()
            await expect(page.locator(".message-status")).to_have_count(0)

            # The Home Screen badge follows the server's unread count.
            state["unread"] = 2
            await page.wait_for_function("() => window.__badges.at(-1) === 2")
            state["unread"] = 0
            await page.wait_for_function("() => window.__badges.at(-1) === 'clear'")

            # The composer starts one line tall and grows with the text.
            height = "() => document.querySelector('#chat-form textarea').offsetHeight"
            one_line = await page.evaluate(height)
            await page.get_by_label("Message to General").fill("one\ntwo\nthree\nfour")
            assert await page.evaluate(height) > one_line
            await page.get_by_label("Message to General").fill("")
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
            await expect(page.get_by_label("Message to General")).to_be_visible()
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
            await expect(night_page.get_by_label("Message to General")).to_be_visible()
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
            state["replies"]["general"].append({**reply, "id": "r3", "created": 1700000002.5, "generation": 1})
            await expect(page.locator(".message-assistant")).to_have_count(3)
            await expect(page.locator(".context-divider")).to_have_count(1)
            assert await page.evaluate("() => document.querySelector('.context-divider').nextElementSibling.textContent.includes('Done.')")
            assert await page.evaluate("() => document.querySelector('#chat-thread').lastElementChild.className") == "message message-assistant"
            # A message the assistant started, such as a reminder, answers no
            # request, so it carries no channel chip; replies keep theirs.
            assert await page.locator(".message-meta span").count() == 0
            state["replies"]["general"].append({**reply, "id": "r4", "created": 1700000003.5, "generation": 1, "reply_to": "u1"})
            await expect(page.locator(".message-meta span")).to_have_count(1)
            await expect(page.locator(".message-meta span")).to_have_text("Web")
            # Blurring the composer restores its inset and shrinks the thread;
            # the thread stays anchored to its end, unless the reader scrolled up.
            # Chromium re-anchors a shrinking scroller by itself, so this cannot
            # fail here without the fix; WebKit (iOS) leaves the offset and ends
            # up an inset above the end, which is what the ResizeObserver fixes.
            await page.add_style_tag(content=":root{--navigation-safe-area:34px}")  # lost on the reloads above
            await page.evaluate("() => document.querySelector('#settings').close()")  # modal would trap focus
            state["replies"]["general"].extend({**reply, "id": f"r{i}", "created": 1700000010 + i, "generation": 1, "text": "Filler line " * 12} for i in range(10, 40))
            await expect(page.locator(".message-assistant")).to_have_count(34)
            gap = "() => { const t = document.querySelector('#chat-thread'); return t.scrollHeight - t.scrollTop - t.clientHeight; }"
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollHeight > document.querySelector('#chat-thread').clientHeight * 2")
            await page.evaluate("() => { const t = document.querySelector('#chat-thread'); t.scrollTop = t.scrollHeight; }")
            await page.get_by_label("Message to General").focus()
            await page.wait_for_function("() => document.querySelector('#chat-thread').scrollHeight - document.querySelector('#chat-thread').scrollTop - document.querySelector('#chat-thread').clientHeight < 1")
            await page.get_by_label("Message to General").blur()
            await page.wait_for_function(f"() => ({gap})() < 1")
            await page.evaluate("() => { document.querySelector('#chat-thread').scrollTop = 0; }")
            await page.get_by_label("Message to General").focus()
            await page.get_by_label("Message to General").blur()
            await asyncio.sleep(0.5)
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollTop") == 0
            # Growing the composer by several lines moves neither an end-anchored
            # thread nor one the reader scrolled up in.
            await page.get_by_label("Message to General").fill("")
            await page.evaluate("() => { const t = document.querySelector('#chat-thread'); t.scrollTop = t.scrollHeight; }")
            await page.get_by_label("Message to General").fill("one\ntwo\nthree\nfour\nfive")
            assert await page.evaluate(f"() => ({gap})()") < 1
            await page.get_by_label("Message to General").fill("")
            await page.evaluate("() => { document.querySelector('#chat-thread').scrollTop = 120; }")
            await page.get_by_label("Message to General").fill("one\ntwo\nthree\nfour\nfive")
            assert await page.evaluate("() => document.querySelector('#chat-thread').scrollTop") == 120
            await page.get_by_label("Message to General").fill("")
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
            await page.get_by_role("button", name="Remove image 2").click()
            await expect(page.locator("#composer-images img")).to_have_count(1)
            await area.fill("What is this?")
            await page.get_by_role("button", name="Send message", exact=True).click()
            await expect(page.locator("#composer-images")).to_be_hidden()
            await expect(area).to_have_value("")
            assert len(uploads) == 1 and uploads[0][0] == "image/jpeg" and uploads[0][1][:3] == b"\xff\xd8\xff", uploads[0][0]
            assert submissions[-1]["text"] == "What is this?" and submissions[-1]["attachments"] == ["attachments/2026-09-15-000001.png"]
            state["replies"]["general"].append({"id": "u9", "space": "general", "role": "user", "text": "What is this?", "status": "done",
                                                "created": 1700000100, "source": "web", "generation": 1,
                                                "metadata": json.dumps({"attachments": ["attachments/2026-09-15-000001.png"]})})
            await expect(page.locator(".message-user .message-images img")).to_have_count(1)
            assert await page.locator(".message-user .message-images a").get_attribute("href") == "/api/attachment?path=attachments%2F2026-09-15-000001.png"
            state["voice"] = True
            await page.reload()
            await expect(page.get_by_role("button", name="Record voice message")).to_be_visible()
            keys = await page.evaluate("() => caches.keys()")
            assert keys == [f"noxide-shell-{instance_version}-test2"], keys
            assert not errors, errors
            await browser.close()
            print("Passed: password-free startup, offline/proxy failure recovery, waiting update, mutation guard, draft-safe multi-tab reload, local draft clearing, mobile overflow, seen acknowledgements, reset dividers, pasted images, voice button.")
    finally:
        release.set()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
