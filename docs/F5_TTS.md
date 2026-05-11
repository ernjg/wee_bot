# F5-TTS Voice Notes

F5-TTS is the local TTS provider for Slack voice notes. The bot generates a normal reply, sends that text to F5-TTS with an agent-specific reference clip, writes a WAV file, and uploads it to Slack with `files_upload_v2`.

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

The F5-TTS runtime dependency is `f5-tts`.

## Reference Voices

Each agent needs a clean reference audio clip plus the exact words spoken in that clip. Short clips around 5 to 15 seconds usually work best.

Suggested local layout:

```text
assets/voices/example/reference.wav
assets/voices/wee/reference.wav
assets/voices/irdnennam/reference.wav
```

Keep these clips out of git if they are private.

## Test F5-TTS Directly

```powershell
"hello from F5-TTS" | .\.venv\Scripts\python.exe scripts\f5_tts_synthesize.py --model F5TTS_v1_Base --ref-audio assets\voices\example\reference.wav --ref-text "Exact transcript of the reference clip." --voice example --language en-us --speed 1.0 --format wav --output .\f5-test.wav
```

Open `.\f5-test.wav` and confirm it plays.

## Agent Config

Each agent can use a different reply model, router model, and F5-TTS reference voice:

```json
{
  "voice": {
    "enabled": true,
    "provider": "f5-tts",
    "python_exe": ".venv/Scripts/python.exe",
    "model": "F5TTS_v1_Base",
    "voice": "example",
    "ref_audio": "assets/voices/example/reference.wav",
    "ref_text": "Exact transcript of the example reference clip.",
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

`max_chars` caps the text sent to TTS after cleanup. `2400` is usually enough for a spoken response around a minute to a minute and a half. Explicit voice-note requests use a spoken-response prompt, so the agent should not shorten poems, toasts, stories, or explanations just because they are audio.

`response_chance` controls random voice notes for ordinary replies. `0.04` means roughly 1 in 25 regular text replies become voice notes. Explicit voice-note requests always use voice when enabled.

## Environment Overrides

Environment variables are fallback defaults. Agent config wins when both are set.

```env
VOICE_ENABLED=1
TTS_PROVIDER=f5-tts
F5_TTS_MODEL=F5TTS_v1_Base
F5_TTS_PYTHON_EXE=.venv\Scripts\python.exe
F5_TTS_VOICE=example
F5_TTS_REF_AUDIO=assets\voices\example\reference.wav
F5_TTS_REF_TEXT=Exact transcript of the example reference clip.
F5_TTS_LANGUAGE=en-us
F5_TTS_SPEED=1.0
VOICE_FORMAT=wav
VOICE_MAX_CHARS=2400
VOICE_RESPONSE_CHANCE=0.04
```

For per-agent overrides, prefix the variable with the agent name:

```env
WEE_F5_TTS_VOICE=wee
WEE_F5_TTS_REF_AUDIO=assets\voices\wee\reference.wav
WEE_F5_TTS_REF_TEXT=Exact transcript of Wee's reference clip.
IRDNENNAM_F5_TTS_VOICE=irdnennam
IRDNENNAM_F5_TTS_REF_AUDIO=assets\voices\irdnennam\reference.wav
IRDNENNAM_F5_TTS_REF_TEXT=Exact transcript of Irdnennam's reference clip.
WEE_OLLAMA_MODEL=qwen2.5:14b
WEE_TOOL_ROUTER_MODEL=qwen2.5:14b
```
