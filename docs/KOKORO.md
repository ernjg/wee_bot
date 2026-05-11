# Kokoro Voice Notes

Kokoro is the local TTS provider for Slack voice notes. The bot generates a normal reply, passes that text into Kokoro ONNX, writes an audio file, and uploads it to Slack with `files_upload_v2`.

## Slack Permission

Add this OAuth scope to the bot token and reinstall the Slack app:

```text
files:write
```

The bot also needs to be a member of any channel where it uploads voice notes.

## Install Dependencies

Install the repo dependencies in the bot virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The Kokoro runtime dependencies are `kokoro-onnx` and `soundfile`.

## Download Model Files

Create a local model directory:

```powershell
New-Item -ItemType Directory -Force C:\tools\kokoro
```

Download these files from the Kokoro ONNX model release:

```text
https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

Place them here:

```text
C:\tools\kokoro\kokoro-v1.0.onnx
C:\tools\kokoro\voices-v1.0.bin
```

## Test Kokoro Directly

```powershell
"hello from kokoro" | .\.venv\Scripts\python.exe scripts\kokoro_synthesize.py --model C:\tools\kokoro\kokoro-v1.0.onnx --voices C:\tools\kokoro\voices-v1.0.bin --voice af_sarah --language en-us --speed 1.0 --format wav --output .\kokoro-test.wav
```

Open `.\kokoro-test.wav` and confirm it plays.

## Agent Config

Each agent can use a different reply model, router model, and Kokoro voice:

```json
{
  "voice": {
    "enabled": true,
    "provider": "kokoro",
    "python_exe": ".venv/Scripts/python.exe",
    "model": "C:/tools/kokoro/kokoro-v1.0.onnx",
    "voices": "C:/tools/kokoro/voices-v1.0.bin",
    "voice": "af_sarah",
    "language": "en-us",
    "speed": 1.0,
    "format": "wav",
    "max_chars": 2400,
    "response_chance": 0.04
  },
  "models": {
    "reply": "qwen2.5:14b",
    "tool_router": "qwen2.5:14b"
  }
}
```

Suggested starting voices:

```text
Wee:       am_puck
Irdnennam: am_echo
```

Other common English voices include `af_sarah`, `af_bella`, `af_heart`, `am_adam`, `am_eric`, and `am_michael`.

`max_chars` caps the text sent to TTS after cleanup. `2400` is usually enough for a spoken response around a minute to a minute and a half. Explicit voice-note requests use a spoken-response prompt, so the agent should not shorten poems, toasts, stories, or explanations just because they are audio.

`response_chance` controls random voice notes for ordinary replies. `0.04` means roughly 1 in 25 regular text replies become voice notes. Explicit voice-note requests always use voice when enabled.

## Environment Overrides

Environment variables are fallback defaults. Agent config wins when both are set.

```env
VOICE_ENABLED=1
TTS_PROVIDER=kokoro
KOKORO_MODEL_PATH=C:\tools\kokoro\kokoro-v1.0.onnx
KOKORO_VOICES_PATH=C:\tools\kokoro\voices-v1.0.bin
KOKORO_PYTHON_EXE=.venv\Scripts\python.exe
KOKORO_VOICE=af_sarah
KOKORO_LANGUAGE=en-us
KOKORO_SPEED=1.0
VOICE_FORMAT=wav
VOICE_MAX_CHARS=2400
VOICE_RESPONSE_CHANCE=0.04
```

For per-agent overrides, prefix the variable with the agent name:

```env
WEE_KOKORO_VOICE=am_puck
IRDNENNAM_KOKORO_VOICE=am_echo
WEE_OLLAMA_MODEL=qwen2.5:14b
WEE_TOOL_ROUTER_MODEL=qwen2.5:14b
```
