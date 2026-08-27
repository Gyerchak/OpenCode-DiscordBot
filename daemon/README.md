# obx_bot — VibeCoder always-on Discord daemon

Keeps the VibeCoder bot online 24/7 in **Bociarnia** and routes channels to the
box's **per-agent brains** (opencode sessions on the shared service):

| Channel | Behavior |
|---|---|
| `#obx-<agent>` (text) | every message answered by `<agent>` |
| `#obx` (text) | default agent, autonomous |
| any other text channel | replies only when **@VibeCoder** is mentioned, the bot is replied-to, or via `/obx` |
| `#obx-<agent>` (voice) | auto-joins when a human is in it → **whisper → agent → kokoro TTS → speaks back** |
| `/obx [agent] [message]` | start a conversation anywhere (default agent unless named) |

## Components
- `obx_bot.py` — the daemon (py-cord 2.8, voice receive via `start_listening` + custom sink)
- `config.json` — box paths, guild/service IDs, whisper/kokoro sockets, voice VAD params
- `agents.json` — the agent roster (title, persona, kokoro voice + lang, enabled)
- venv: `~/ai-tools/discordbot` · launcher: `box/scripts/obx-discordbot-daemon.sh`

## Run
```bash
apps/obx-discordbot-daemon start     # or box/scripts/obx-discordbot-daemon.sh start
apps/obx-discordbot-daemon status    # pid + log tail  (.)/logs/obx-discordbot-daemon.log
apps/obx-discordbot-daemon doctor    # tokens, service, intents, servers
apps/obx-discordbot-daemon stop|restart
```

## How the brains work
- One opencode session per agent, titled `obx-<agent>`, created on the box shared
  service (127.0.0.1:49374, `POST /api/session`), answered via
  `POST /api/session/{id}/generate` → `{"data":{"text"}}`. Session ids persist in
  `daemon/state.json` and sessions keep their own conversation history.
- Persona prompts in `agents.json` set each agent's character; swap them anytime,
  add new agents by adding entries (new `#obx-<name>` channels pick them up).

## Caveats (read!)
1. **Message content LIMITED**: this app is unverified, so Discord only delivers
   message *content* when the bot is mentioned, replied-to, or DM'd. Passive
   "read every obx-* message without being addressed" requires **verification** of
   the application in the Discord Developer Portal (user action). Until then,
   address the bot in `obx-*` channels (mention/reply) — replies still work.
2. **Voice receive vs DAVE**: Discord's E2EE (DAVE) rollout can block voice
   *listening* on some servers. Send works regardless. If the log shows
   "voice listen failed", passive voice listening is blocked server-side.
3. The bot ignores itself and other bots (no echo loops).
