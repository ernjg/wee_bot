# Wee Bot

Wee Bot is a local Slack bot that uses Ollama as its language model backend. The bot code runs from this repo, while Ollama runs separately in the background at `http://localhost:11434`.

The current entry point is `bot.py`. Agent-specific settings, memory, and personality files live under `agents/<agent-name>/`.

Shout out to Jconmerc for inspiring this project.

## Setup

Clone the repo:

```powershell
git clone https://github.com/ernjg/wee_bot.git
cd wee_bot
```

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then install dependencies:

```powershell
pip install -r requirements.txt
```

## Environment Variables

Copy `.env.example` to `.env` and fill in your real Slack tokens:

```powershell
Copy-Item .env.example .env
```

The bot supports per-agent token variables. For example, an agent named `wee` can use:

```env
WEE_SLACK_BOT_TOKEN=xoxb-your-bot-token
WEE_SLACK_APP_TOKEN=xapp-your-socket-mode-token
```

Shared runtime settings can also go in `.env`. Prefer per-agent model variables so each agent can use its own reply model and tool-router model:

```env
WEE_OLLAMA_MODEL=qwen2.5:14b
WEE_TOOL_ROUTER_MODEL=qwen2.5:14b
IRDNENNAM_OLLAMA_MODEL=qwen2.5:14b
IRDNENNAM_TOOL_ROUTER_MODEL=qwen2.5:14b
DEBUG=1
EVENT_LOG_ENABLED=1
EVENT_LOG_PATH=logs/wee.events.jsonl
OLLAMA_TIMEOUT_SECONDS=90
OLLAMA_NUM_PREDICT=120
OLLAMA_NUM_CTX=4096
OLLAMA_TEMPERATURE=0.7
AMBIENT_RESPONSE_CHANCE=0.15
THREAD_AMBIENT_RESPONSE_CHANCE=0.35
REACTION_RESPONSE_CHANCE=0.65
VOICE_ENABLED=0
TTS_PROVIDER=f5-tts
F5_TTS_MODEL=F5TTS_v1_Base
F5_TTS_PYTHON_EXE=.venv\Scripts\python.exe
F5_TTS_VOICE=example
F5_TTS_REF_AUDIO=assets\voices\example\reference.wav
F5_TTS_REF_TEXT=TODO: exact transcript of the reference audio.
F5_TTS_LANGUAGE=en-us
F5_TTS_SPEED=1.0
VOICE_FORMAT=wav
VOICE_MAX_CHARS=2400
VOICE_RESPONSE_CHANCE=0.04
```

Do not commit `.env`. It contains private Slack tokens.

## Agent Files

Each real agent should have its own folder:

```text
agents/
  example/
    config.json
    memory.json
    personality.txt
```

`agents/example/` is a safe committed template. Copy it for a real agent:

```powershell
Copy-Item agents\example agents\wee -Recurse
```

Then edit `agents\wee\config.json`, `agents\wee\personality.txt`, and `agents\wee\memory.json`.

The important config fields are:

```json
{
  "display_name": "Example Agent",
  "personality_file": "personality.txt",
  "memory_file": "memory.json",
  "slack_bot_token_env": "EXAMPLE_SLACK_BOT_TOKEN",
  "slack_app_token_env": "EXAMPLE_SLACK_APP_TOKEN",
  "triggers": ["example", "example agent"],
  "explicit_only_channels": ["announcements", "announce"],
  "ambient_channel_chances": {
    "announce": 0.03
  },
  "enabled_tools": [
    "get_time",
    "search_memory",
    "update_memory",
    "search_relationship_memory",
    "update_relationship_memory",
    "summarize_thread",
    "get_channel_context",
    "get_user_profile",
    "list_recent_threads",
    "save_thread_summary",
    "search_channel_history",
    "quote_recent_message",
    "get_message_permalink",
    "inspect_reactions",
    "set_reminder_note",
    "react_to_message",
    "add_reaction_set",
    "read_link_preview",
    "search_web",
    "send_voice_note"
  ],
  "voice": {
    "enabled": false,
    "provider": "f5-tts",
    "python_exe": ".venv/Scripts/python.exe",
    "model": "F5TTS_v1_Base",
    "voice": "example",
    "ref_audio": "assets/voices/example/reference.wav",
    "ref_text": "TODO: exact transcript of the example reference audio.",
    "language": "en-us",
    "speed": 1.0,
    "format": "wav",
    "max_chars": 2400,
    "response_chance": 0.04
  },
  "models": {
    "reply": "llama3.1:8b",
    "tool_router": "llama3.1:8b"
  }
}
```

Local real-agent folders such as `agents/wee/` are ignored by Git. Only `agents/example/` is meant to be committed.

## Starting Ollama

Check that Ollama is available:

```powershell
ollama list
```

Pull a model if needed:

```powershell
ollama pull llama3.1:8b
```

Test the model manually:

```powershell
ollama run llama3.1:8b
```

Exit the Ollama chat with:

```text
/bye
```

Ollama serves requests locally at:

```text
http://localhost:11434
```

## Running The Bot

Activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Run an agent by name:

```powershell
python bot.py --agent wee
```

You can also use the `AGENT` environment variable:

```powershell
$env:AGENT = "wee"
python bot.py
```

If no agent is provided, `bot.py` defaults to the committed `example` agent. A real Slack run should usually pass `--agent` or set `AGENT`.

Leave the terminal open while the bot is running.

## Event Logging

For debugging, enable structured local JSONL logs:

```env
EVENT_LOG_ENABLED=1
EVENT_LOG_PATH=logs/wee.events.jsonl
```

Each line records one event or action, such as inbound messages, response decisions, ignored reasons, tool-router decisions, tool results, memory command replies, and final replies. Logs are local runtime files and ignored by Git. If multiple agents share `EVENT_LOG_PATH`, each agent writes to its own file by prefixing the filename with the agent name; set `WEE_EVENT_LOG_PATH` or `IRDNENNAM_EVENT_LOG_PATH` for exact per-agent paths.

## Response Behavior

The bot does not answer every message that contains its name. It distinguishes direct address from passive mentions, so a message like `wee, what do you think?` is treated differently from `I asked Wee earlier`.

It can also join open room prompts without being named, such as `anyone know?` or `thoughts?`. Ambient replies are intentionally probabilistic and can be tuned in `.env`:

```env
AMBIENT_RESPONSE_CHANCE=0.15
THREAD_AMBIENT_RESPONSE_CHANCE=0.35
```

Lower these values to make agents quieter. Raise them to make agents more socially active.
`REACTION_RESPONSE_CHANCE` controls how often the agent follows through on router-selected reactions to messages that do not explicitly ask for a reply. The router chooses the emoji from message intent and context, so reactions are not locked to a fixed phrase list.

Active thread state is kept only so agents can follow up in recently active threads. It is pruned on memory saves. The defaults are intentionally conservative now: `ACTIVE_THREAD_TTL_SECONDS=14400` (4 hours) and `MAX_ACTIVE_THREADS=25`. Lower these if `memory.json` is still too noisy.

Bot-originated messages are allowed through the event layer so agents can talk to each other. Ambient replies to bot messages are suppressed; another bot has to address the agent directly.

Some channels should stay explicit-only, such as announcements. Configure those per agent:

```json
{
  "explicit_only_channels": ["announcements", "announce", "C0123456789"]
}
```

Entries can be channel names, `#channel-name`, or Slack channel IDs. In explicit-only channels, direct mentions and direct questions still work, but ambient replies are disabled.

If you want a channel to allow rare ambient behavior instead of fully disabling it, configure a per-channel chance. This is useful for announcement channels where an occasional reaction or joke is funny, but normal ambient behavior is too loud:

```json
{
  "ambient_channel_chances": {
    "announce": 0.03,
    "C0123456789": 0.03
  }
}
```

Use channel IDs when the bot does not have `channels:read`; otherwise it cannot reliably map a Slack channel ID back to a channel name.

## Tools

Agents can use a small set of built-in tools when a tool-router model decides they are needed:

```json
{
  "enabled_tools": [
    "get_time",
    "search_memory",
    "update_memory",
    "search_relationship_memory",
    "update_relationship_memory",
    "summarize_thread",
    "get_channel_context",
    "get_user_profile",
    "list_recent_threads",
    "save_thread_summary",
    "search_channel_history",
    "quote_recent_message",
    "get_message_permalink",
    "inspect_reactions",
    "set_reminder_note",
    "react_to_message",
    "add_reaction_set",
    "read_link_preview",
    "search_web",
    "send_voice_note"
  ]
}
```

Available tools:

- `get_time`: answer date/time questions.
- `search_memory`: find relevant memories without dumping everything.
- `update_memory`: save one structured memory claim.
- `search_relationship_memory`: find relevant relationship memories, social context, friendships, rivalries, and inside jokes.
- `update_relationship_memory`: save one structured relationship memory with optional people names.
- `summarize_thread`: summarize the current Slack thread.
- `get_channel_context`: inspect recent channel messages for "what did I miss?" style questions.
- `get_user_profile`: look up a Slack user's basic profile.
- `list_recent_threads`: list recently active threads in the current channel.
- `save_thread_summary`: save a compact memory summary of a useful thread.
- `search_channel_history`: search recent messages in the current channel.
- `quote_recent_message`: find and quote recent channel messages by topic or speaker.
- `get_message_permalink`: get a Slack link to the latest, thread root, or specified message.
- `inspect_reactions`: inspect emoji reactions on a message.
- `set_reminder_note`: save a local reminder note with optional due text.
- `react_to_message`: add an emoji reaction to the latest or specified message. The router can choose this implicitly for celebrations, agreement, thanks, jokes, or lightweight acknowledgement.
- `add_reaction_set`: add 1-3 emoji reactions when a cluster feels more natural than one reaction, such as celebrations or big wins.
- `read_link_preview`: fetch the title, description, and a short excerpt from a URL someone posted.
- `search_web`: search public web results for current facts when Slack context and memory are insufficient.
- `send_voice_note`: generate a normal agent reply, pass that text through TTS, and upload it to Slack as a playable audio file. It is opt-in per agent.

Tool use is intent-based, not tied to exact key phrases. A separate router model sees the latest message plus recent Slack context and returns either `none` or a structured tool call. For example, `what time is it?`, `when was that?`, and `how long ago did this happen?` can all route through `get_time` when current time would help. Recent Slack context includes compact timestamps so the agent can reason about elapsed time and recaps.

For reactions, add Slack scopes `reactions:write` and `reactions:read`. The bot checks existing reactions when it can, so it avoids adding the same emoji twice.

For voice notes, add Slack scope `files:write` and reinstall the Slack app. Slack does not provide a separate bot API for native human-style voice-note recording; the bot uploads an audio file with `files_upload_v2`, which Slack displays as a playable clip. Voice generation uses local F5-TTS when `voice.enabled` is true for the agent. Each agent can use a different custom reference clip and transcript through `ref_audio` and `ref_text`. Explicit voice-note requests use a spoken-response prompt, so poems, toasts, stories, and explanations can be longer than ordinary chat replies. Otherwise normal text replies become voice notes with `VOICE_RESPONSE_CHANCE`, default `0.04` or 1 in 25. See [docs/F5_TTS.md](docs/F5_TTS.md) for setup.

`models.tool_router` defaults to `models.reply` when omitted. You can point it at a smaller/faster Ollama model if tool routing latency becomes annoying. The older top-level `ollama_model` and `tool_router_model` fields still work, but new configs should use the `models` object.

If `enabled_tools` is omitted, all built-in tools are enabled. Remove tools from the list to disable them for a specific agent.

## Testing Behavior Locally

Use the fake Slack harness to test routing and reply behavior without starting Socket Mode or sending real Slack messages:

```powershell
python scripts\fake_slack_tester.py
```

The default suite covers casual direct chat, time and relative-time routing, channel catch-up, thread summaries, memory lookup and updates, user profiles, active threads, channel search, reminders, voice-note disabled/upload paths, random voice-note conversion, ambient reactions, passive mentions, explicit-only channels, bot-to-bot direct replies, thread replies, invalid router/tool pairings, low-confidence routes, and reaction sampling. You can pass your own fake messages as arguments:

```powershell
python scripts\fake_slack_tester.py "wee, welcome back" "we shipped the fix" "wee, what did I miss?"
```

The script prints pass/fail results for the default suite. With custom messages, it prints the response decision, any posted messages, and any reactions for each case. It monkeypatches the model call with a deterministic fake router, so it is meant for behavior regression checks rather than model-quality evaluation.

## Stopping

To stop the Slack bot, click into the terminal where it is running and press:

```text
Ctrl + C
```

This stops the Python bot script. It does not necessarily stop Ollama.

To stop Ollama on Windows:

```powershell
taskkill /IM ollama.exe /F
```

To check whether Ollama is still listening on port `11434`:

```powershell
netstat -ano | findstr :11434
```

## Common Errors

### `Missing agent config`

The requested agent folder does not exist, or it does not contain `config.json`.

For `--agent wee`, make sure this file exists:

```text
agents/wee/config.json
```

### `Missing Slack bot token`

The configured bot token environment variable is missing from `.env`.

If `agents/wee/config.json` says:

```json
"slack_bot_token_env": "WEE_SLACK_BOT_TOKEN"
```

then `.env` must include:

```env
WEE_SLACK_BOT_TOKEN=xoxb-your-token
```

### `Missing Slack app token`

The configured Socket Mode app token environment variable is missing from `.env`.

The app token should start with `xapp-`, not `xoxb-`.

### Ollama Connection Error

If the bot cannot connect to Ollama, check that Ollama is running:

```powershell
ollama list
```

Then test the model:

```powershell
ollama run llama3.1:8b
```

If that fails, the problem is with Ollama or the model, not Slack.

### Slack `missing_scope` Error

If Slack returns `missing_scope`, the Slack app needs another OAuth permission.

Go to:

```text
Slack API -> Your App -> OAuth & Permissions -> Bot Token Scopes
```

Add the missing scope, then reinstall the app to the workspace.

## Useful Commands

```powershell
ollama list
ollama pull llama3.1:8b
ollama run llama3.1:8b
python bot.py --agent wee
```
