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
DEBUG=1
OLLAMA_TIMEOUT_SECONDS=90
OLLAMA_NUM_PREDICT=120
OLLAMA_NUM_CTX=4096
OLLAMA_TEMPERATURE=0.7
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
  "ollama_model": "llama3.1:8b"
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
