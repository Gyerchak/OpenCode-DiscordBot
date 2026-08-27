# Privacy Policy

**Effective date:** 2026-08-27

This privacy policy explains what VibeCoder ("the bot", "we", "the operator")
collects and how it is handled when you use the bot in a Discord server.

## Who operates the bot
VibeCoder is operated by the OpenCodeBox project (maintainer: Gyerchak) as a
personal assistant bot for the server(s) it is installed in.

## What the bot collects and why

### 1. Discord messages and message content
When the bot is running it can, depending on the app's granted access,
receive messages in the Discord servers it is installed in — including:

- the text of messages sent in channels the bot participates in
  (`obx-*` channels, channels where it is mentioned or replied to, and
  `/obx` conversations),
- the sender's Discord username and avatar,
- channel/server names and IDs.

**Purpose:** to answer you, run the named agent for the channel you are in,
and keep the conversation context that agent needs.

### 2. Voice transcripts
When the bot joins a voice channel (channels named `obx-*`), it may
transcribe the speech of people in that channel using a local speechto-text
engine (faster-whisper) so it can reply. These transcripts are processed
**locally on the operator's machine** and are treated like any other message.

### 3. Conversation context ("sessions")
Your messages/transcripts are passed to the bot's "agent" sessions — an AI
model. The conversations become part of the bot's session history stored on
the operator's private computer. Some AI requests are handled by hosted LLM
providers (e.g. the OpenCodeBox AI providers) solely to produce the bot's
reply.

### 4. Logs
The bot keeps operational logs (timestamps, channel names, message snippets,
errors) on the operator's machine to debug and improve the bot.

## What we do NOT do
- We do not sell or rent any data.
- We do not show ads or use third-party advertising/tracking/analytics.
- We do not collect data from servers the bot is not installed in.
- We do not intentionally collect data of anyone under 13 years of age; the
  bot is subject to Discord's Terms of Service.

## Retention
Conversation history and logs are kept on the operator's private machine.
You can ask to have data about you removed (see below).

## Access, correction and deletion
To request a copy of, or deletion of, data concerning you, contact the
operator via Discord: **Gyerchak** (or via the server where you use the bot).
We will act on reasonable requests.

## Data sharing
Your messages may transit Discord's servers (unavoidable — the bot operates
inside Discord) and, for producing replies, the LLM provider we use at the
time. No other sharing occurs.

## Changes
We may update this policy; the effective date above always reflects the
latest version.

## Contact
Discord: **Gyerchak** — OpenCodeBox project.
