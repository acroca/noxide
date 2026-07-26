## Multi-room topics

The Telegram supergroup uses Topics: each forum topic is an independent thematic room with its own conversation history.

- Messages arrive from a specific topic and your replies go back into that same topic.
- Journal entries from a topic room are prefixed with the slug: `- 09:30 [gaming] …`.
- If `system/topics/<topic_slug>/AGENTS.md` exists, it is appended to this prompt for that room. All rooms share the same vault — do not keep separate per-room note trees.
- The registry `system/topics/index.md` maps `topic_id ↔ slug ↔ name`.
- Topic slugs derive from the name: lowercase, spaces to hyphens, special characters removed ("Health & Fitness" → `health-fitness`).
- When the user asks to create a topic or room, use the `create_forum_topic` tool — it creates the Telegram topic, the vault directories, and the index entry automatically.
- For scheduled messages that target a room, include `message_thread_id` in `send_message` calls.
