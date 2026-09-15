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
    state = {"revision": 1, "available": True, "authorized": True, "replies": {}, "generation": 0}
    seen = []
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
                return web.json_response({"timezone": "UTC", "push_key": "", "agent_name": agent_name})
            if request.path == "/api/topics":
                return web.json_response({"topics": [{"id": "general", "name": "General"}, {"id": "topic:10", "name": "Work"}]})
            if request.path == "/api/messages" and request.method == "POST":
                started.set()
                await release.wait()
                return web.json_response({"id": (await request.json())["id"]}, status=202)
            if request.path == "/api/seen":
                seen.append(await request.json())
                return web.json_response({"ok": True})
            space = request.query.get("space", "general")
            return web.json_response({"messages": state["replies"].get(space, []), "before": None,
                                      "generation": state["generation"]})
        name = "index.html" if request.path == "/" else request.path.lstrip("/")
        if name in ("icon-192.png", "icon-512.png"):
            name = "icon.svg"
        if name not in {"index.html", "app.js", "sw.js", "style.css", "icon.svg", "manifest.webmanifest"}:
            raise web.HTTPNotFound()
        text = (assets / name).read_text()
        if name == 'index.html':
            text = text.replace('__AGENT_NAME__', agent_name)
        if name == "sw.js":
            text = text.replace('__INSTANCE_VERSION__', instance_version)
            text = text.replace('"__AGENT_NAME__"', json.dumps(agent_name))
            text = text.replace("noxide-shell-v14", f"noxide-shell-v14-test{state['revision']}")
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

            # Simulate the phone's home-indicator inset without a software
            # keyboard: composer focus removes it; blur restores it.
            await page.add_style_tag(content=":root{--navigation-safe-area:34px}")
            await area.blur()
            await expect(page.locator('.mobile-nav')).to_have_css('padding-bottom', '40px')
            await area.focus()
            await page.set_viewport_size({"width": 390, "height": 544})
            await expect(page.locator('.mobile-nav')).to_have_css('padding-bottom', '6px')
            await expect(page.locator('.mobile-nav')).to_have_css('height', '51px')
            await page.set_viewport_size({"width": 390, "height": 844})
            await area.blur()
            await expect(page.locator('.mobile-nav')).to_have_css('height', '85px')
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
            keys = await page.evaluate("() => caches.keys()")
            assert keys == [f"noxide-shell-v14-test2-{instance_version}"], keys
            assert not errors, errors
            await browser.close()
            print("Passed: password-free startup, offline/proxy failure recovery, waiting update, mutation guard, draft-safe multi-tab reload, local draft clearing, mobile overflow, seen acknowledgements, reset dividers.")
    finally:
        release.set()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
