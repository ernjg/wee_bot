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

Shared runtime settings can also go in `.env`:

```env
OLLAMA_MODEL=llama3.1:8b
TOOL_ROUTER_MODEL=llama3.1:8b
DEBUG=1
EVENT_LOG_ENABLED=1
EVENT_LOG_PATH=logs/wee.events.jsonl
OLLAMA_TIMEOUT_SECONDS=90
OLLAMA_NUM_PREDICT=120
OLLAMA_NUM_CTX=4096
OLLAMA_TEMPERATURE=0.7
AMBIENT_RESPONSE_CHANCE=0.15
THREAD_AMBIENT_RESPONSE_CHANCE=0.35
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
  "enabled_tools": [
    "get_time",
    "search_memory",
    "update_memory",
    "summarize_thread",
    "get_channel_context",
    "get_user_profile",
    "list_recent_threads",
    "save_thread_summary",
    "search_channel_history",
    "set_reminder_note",
    "react_to_message"
  ],
  "ollama_model": "llama3.1:8b",
  "tool_router_model": "llama3.1:8b"
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

Bot-originated messages are allowed through the event layer so agents can talk to each other. Ambient replies to bot messages are suppressed; another bot has to address the agent directly.

Some channels should stay explicit-only, such as announcements. Configure those per agent:

```json
{
  "explicit_only_channels": ["announcements", "announce", "C0123456789"]
}
```

Entries can be channel names, `#channel-name`, or Slack channel IDs. In explicit-only channels, direct mentions and direct questions still work, but ambient replies are disabled.

## Tools

Agents can use a small set of built-in tools when a tool-router model decides they are needed:

```json
{
  "enabled_tools": [
    "get_time",
    "search_memory",
    "update_memory",
    "summarize_thread",
    "get_channel_context",
    "get_user_profile",
    "list_recent_threads",
    "save_thread_summary",
    "search_channel_history",
    "set_reminder_note",
    "react_to_message"
  ]
}
```

Available tools:

- `get_time`: answer date/time questions.
- `search_memory`: find relevant memories without dumping everything.
- `update_memory`: save one structured memory claim.
- `summarize_thread`: summarize the current Slack thread.
- `get_channel_context`: inspect recent channel messages for "what did I miss?" style questions.
- `get_user_profile`: look up a Slack user's basic profile.
- `list_recent_threads`: list recently active threads in the current channel.
- `save_thread_summary`: save a compact memory summary of a useful thread.
- `search_channel_history`: search recent messages in the current channel.
- `set_reminder_note`: save a local reminder note with optional due text.
- `react_to_message`: add an emoji reaction to the latest or specified message. The router can choose this implicitly for celebrations, agreement, thanks, jokes, or lightweight acknowledgement.

Tool use is intent-based, not tied to exact key phrases. A separate router model sees the latest message plus recent Slack context and returns either `none` or a structured tool call. For example, `what time is it?`, `when was that?`, and `how long ago did this happen?` can all route through `get_time` when current time would help. Recent Slack context includes compact timestamps so the agent can reason about elapsed time and recaps.

For reactions, add Slack scopes `reactions:write` and `reactions:read`. The bot checks existing reactions when it can, so it avoids adding the same emoji twice.

`TOOL_ROUTER_MODEL` defaults to the main model when omitted. You can point it at a smaller/faster Ollama model if tool routing latency becomes annoying.

If `enabled_tools` is omitted, all built-in tools are enabled. Remove tools from the list to disable them for a specific agent.

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
