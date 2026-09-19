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
from assistant.tools import VaultTools


async def main():
    with tempfile.TemporaryDirectory(prefix="noxide-preview-") as directory:
        root = Path(directory)
        vault = VaultTools(root / "vault")
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
        # The chat is threads: a root and its replies. Most are two messages;
        # a longer one shows the marked, indented form.
        captured = archive.insert("general", "user", "A message captured in Telegram: watered the balcony plants.", "done",
                                  source="telegram")
        archive.insert("general", "assistant", "Noted — the herbs will be glad of it.", "done", reply_to=captured,
                       source="telegram")
        opener = archive.insert("general", "user", "Can we plan the mountain walk?", "done")
        question = archive.insert("general", "assistant", "Of course. Saturday or Sunday?", "done", reply_to=opener)
        answer = archive.insert("general", "user", "Saturday, leaving early.", "done", reply_to=question)
        archive.insert("general", "assistant", "Saturday it is; I've noted an early start on the weekend page.", "done",
                       reply_to=answer)
        cfg = Config(state_dir=root, vault_path=root / "vault", pwa_origin="http://localhost:8080",
                     timezone="Europe/Madrid")
        service = Companion(cfg, agent, vault, archive=archive)
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
