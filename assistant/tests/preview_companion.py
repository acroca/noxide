"""Local UI preview using a temporary example vault and a fake agent. Never production data.

Run with `mise exec uv -- uv run python -m tests.preview_companion`.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from assistant import copilot
from assistant.agent import Agent
from assistant.companion import Companion
from assistant.config import Config
from assistant.conversations import ConversationArchive
from assistant.schedule import Scheduler
from assistant.tools import VaultTools


async def main():
    with tempfile.TemporaryDirectory(prefix="noxide-preview-") as directory:
        root = Path(directory)
        vault = VaultTools(root / "vault")
        vault.write_file("system/topics/index.md", "# Topics\n\n| topic_id | slug | name |\n|---|---|---|\n| 10 | work | Work |\n| 20 | family | Family |\n| 30 | health | Health |\n")
        vault.write_file("system/topics/work/AGENTS.md", "Focus on work in this topic.")
        vault.write_file("wiki/now.md", """# Now
## Today · Monday, September 14
- [ ] Send the studio proposal (due 2026-09-14)
- 16:00 — A walk with Alex, by the river
- [ ] Give the balcony plants a little water

## Upcoming
- Tuesday — Book club at Nora’s (2026-09-15)
- Friday — Weekend in the mountains (2026-09-18)

## Waiting
- The studio team is reviewing the moodboard.
""")
        projects = [
            ("studio", "A new studio", "Shaping a calmer place to do good work. The first proposal is nearly ready.", "Send the studio proposal"),
            ("garden", "The balcony garden", "A little green in the everyday. Keeping the herbs happy as the seasons change.", "Water the plants"),
            ("mountains", "A weekend away", "Two days in the mountains. The cabin is booked; now for the small details.", "Plan the walking route"),
            ("reading", "Between the pages", "Making more room to read. This month: a book worth slowing down for.", "Finish the last chapter"),
        ]
        for slug, title, status, task in projects:
            vault.write_file(f"wiki/projects/{slug}.md", f"# {title}\n\n**Status:** {status}\n\n## Tasks\n- [ ] {task}\n\n## Notes\nKeep the next step small and specific.")

        async def reply(*args, **kwargs):
            await asyncio.sleep(2)
            return "Your note is here.\n\nThis is the **preview assistant**, so no model was called and no vault changes were made. In the real service, this conversation uses your existing assistant and vault tools."

        archive = ConversationArchive(root)
        agent = Agent(vault, archive=archive, home_chat_fn=lambda: 123)

        async def chat(*args, **kwargs):
            text = await reply()
            return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}

        copilot._client = MagicMock(chat=AsyncMock(side_effect=chat))
        archive.insert("general", "user", "A message captured in Telegram.", "done", source="telegram")
        scheduler = Scheduler(vault, AsyncMock(), tz_name="Europe/Madrid")
        scheduler.schedule("2099-09-15T09:00:00", "Take the book along for tonight’s book club.", False)
        scheduler.schedule("0 8 * * MON", "A gentle Monday check-in: what matters this week?", True)
        cfg = Config(state_dir=root, vault_path=root / "vault", pwa_origin="http://localhost:8080",
                     timezone="Europe/Madrid")
        service = Companion(cfg, agent, vault, scheduler, archive=archive)
        runner = web.AppRunner(service.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8080).start()
        try:
            await asyncio.Event().wait()
        finally:
            await service.close()
            await runner.cleanup()
            archive.close()


if __name__ == "__main__":
    asyncio.run(main())
