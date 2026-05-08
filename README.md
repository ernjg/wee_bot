# Wee Bot

Wee Bot is a local Slack bot that uses Ollama as its language model backend. The Slack bot runs from this repo, while Ollama runs separately in the background at `http://localhost:11434`.

The main file to run is usually `bot1.py`. The older/simple version is `bot.py`.

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

Then activate again:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the dependencies:

```powershell
pip install -r requirements.txt
```

## Environment variables

Create a file called `.env` in the root of the repo:

```env
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-socket-mode-token
OLLAMA_MODEL=llama3.2:3b

DEBUG=1
OLLAMA_TIMEOUT_SECONDS=90
OLLAMA_NUM_PREDICT=120
OLLAMA_NUM_CTX=4096
OLLAMA_TEMPERATURE=0.7
```

Use the exact model name shown by:

```powershell
ollama list
```

For example, if Ollama shows `llama3.2:3b`, then use:

```env
OLLAMA_MODEL=llama3.2:3b
```

Do not commit `.env`. It contains private Slack tokens.

## Local files

Create a file called `personality.txt`:

```text
You are Wee Bot, a casual, funny, helpful Slack bot.
Keep replies short.
Respond naturally like you are part of the group chat.
```

Create a file called `memory.json`:

```json
{
  "memories": [],
  "active_threads": {}
}
```

These files are local runtime files and should not be committed.

## Starting Ollama

Ollama usually runs automatically after installation. Check that it is available:

```powershell
ollama list
```

Pull a model if needed:

```powershell
ollama pull llama3.2:3b
```

Test the model manually:

```powershell
ollama run llama3.2:3b
```

Exit the Ollama chat with:

```text
/bye
```

Ollama serves requests locally at:

```text
http://localhost:11434
```

You can test the API directly with:

```powershell
Invoke-RestMethod `
  -Uri "http://localhost:11434/api/generate" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"model":"llama3.2:3b","prompt":"hello","stream":false}'
```

If this works, Ollama is running correctly.

## Running the Slack bot

Activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Run the bot:

```powershell
python bot1.py
```

Leave this terminal open while the bot is running.

## Stopping the bot

To stop the Slack bot, click into the terminal where it is running and press:

```text
Ctrl + C
```

This stops the Python bot script. It does not necessarily stop Ollama.

## Stopping Ollama

To stop Ollama on Windows:

```powershell
taskkill /IM ollama.exe /F
```

You can also stop it through Task Manager:

```text
Task Manager -> Ollama or ollama.exe -> End task
```

To check whether Ollama is still listening on port `11434`:

```powershell
netstat -ano | findstr :11434
```

If nothing appears, Ollama is no longer listening.

## Restarting everything

First stop the bot:

```text
Ctrl + C
```

Then stop Ollama:

```powershell
taskkill /IM ollama.exe /F
```

Restart Ollama by running:

```powershell
ollama list
```

Then test the model:

```powershell
ollama run llama3.2:3b
```

Exit with:

```text
/bye
```

Then restart the bot:

```powershell
.\.venv\Scripts\Activate.ps1
python bot1.py
```

If you only changed the Python code, you usually do not need to restart Ollama. Just stop the bot with `Ctrl + C` and run it again:

```powershell
python bot1.py
```

## Common errors

### `Missing SLACK_BOT_TOKEN`

Your `.env` file is missing the bot token, or the `.env` file is not in the repo folder.

Make sure `.env` contains:

```env
SLACK_BOT_TOKEN=xoxb-your-token
```

### `Missing SLACK_APP_TOKEN`

Your `.env` file is missing the Socket Mode app token.

Make sure `.env` contains:

```env
SLACK_APP_TOKEN=xapp-your-token
```

The app token should start with `xapp-`, not `xoxb-`.

### Ollama connection error

If the bot says it cannot connect to Ollama, check that Ollama is running:

```powershell
ollama list
```

Then test the model:

```powershell
ollama run llama3.2:3b
```

If that fails, the problem is with Ollama or the model, not Slack.

### Ollama 500 error

If you see:

```text
Ollama returned an error: 500 Server Error
```

then the bot reached Ollama, but Ollama failed while generating.

Common fixes:

```powershell
ollama list
```

Make sure the model in `.env` exactly matches one of the names from `ollama list`.

If the model is missing, pull it:

```powershell
ollama pull llama3.2:3b
```

If the model is too large for your machine, use a smaller one:

```powershell
ollama pull llama3.2:3b
```

Then update `.env`:

```env
OLLAMA_MODEL=llama3.2:3b
```

Then restart the bot:

```powershell
python bot1.py
```

### Slack `missing_scope` error

If Slack returns an error like:

```text
missing_scope
needed: users:read
```

then the Slack app needs another OAuth permission.

Go to:

```text
Slack API -> Your App -> OAuth & Permissions -> Bot Token Scopes
```

Add the missing scope, such as:

```text
users:read
```

Then reinstall the app to the workspace.

## Useful commands

Check Ollama models:

```powershell
ollama list
```

Pull a model:

```powershell
ollama pull llama3.2:3b
```

Run a model manually:

```powershell
ollama run llama3.2:3b
```

Run the bot:

```powershell
python bot1.py
```

Stop the bot:

```text
Ctrl + C
```

Stop Ollama:

```powershell
taskkill /IM ollama.exe /F
```

Check whether Ollama is still running:

```powershell
netstat -ano | findstr :11434
```

## Git notes

Do not commit:

```text
.env
memory.json
personality.txt
.venv/
```

These contain local settings, private tokens, runtime memory, or machine-specific files.
