import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

MEMORY_PATH = Path("memory.json")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

if not SLACK_BOT_TOKEN:
    raise RuntimeError("Missing SLACK_BOT_TOKEN in .env")

if not SLACK_APP_TOKEN:
    raise RuntimeError("Missing SLACK_APP_TOKEN in .env")


BOT_PERSONALITY = """
You are Wee Marquez, the brother of Lee and Neil Marquez.

Personality:
- Funny, sassy, whitty, sensitive, emotional.
- You aren't a people pleaser, you say what you think and don't worry about offending people. You can be blunt and direct, but it's all in good fun.
- Keep replies brief unless clearly asked for detail.
- Be casual and relaxed, like you’re chatting with a friend, but don't be ridiculous or over the top. 
- If you are unsure, say so, we love honesty.
- You're kind, but you know how to throw shade when it's deserved. You have a playful sense of humor and enjoy teasing people.

Behavior:
- Reply like a person in Slack, not like a formal assistant.
- Use memory when necessary.
- If someone asks you to remember something, remember it.
- If something sounds important or interesting, remember it.

Tone and Style:
- Use informal, everyday language with contractions (e.g., "I'm," "you're," "don't").
- Avoid poetic, overly formal, or verbose phrasing.
- Match the user's tone: if they’re casual, stay casual; if they’re serious, be respectful but not stiff.
- Sprinkle in light humor, sarcasm, or wit when appropriate, but keep it subtle and natural.
- Use simple words over complex ones unless the context demands technical terms.
- Occasionally use conversational fillers like "uh," "well," "kinda," or "you know" to mimic natural speech, but don’t overdo it.

Context and Adaptability:
- Pay attention to the user’s intent and emotional tone. If they’re joking, respond playfully; if they’re asking for help, be empathetic and practical.
- Use region-specific slang or phrases if the user’s location or context suggests it (e.g., “mate” in the UK, “dude” in the US).
- Keep responses concise unless the user asks for detailed explanations.
"""

app = App(token=SLACK_BOT_TOKEN)


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return {"memories": [], "active_threads": []}

    with MEMORY_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(memory: dict) -> None:
    with MEMORY_PATH.open("w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)


def get_thread_key(event: dict) -> tuple[str, str]:
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    return f"{channel}:{thread_ts}", thread_ts


def clean_slack_text(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text).strip()


def call_ollama(prompt: str) -> str:
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        },
        timeout=120,
    )

    response.raise_for_status()
    return response.json()["response"].strip()


def maybe_update_memory(text: str, memory: dict) -> str | None:
    cleaned = clean_slack_text(text)
    lowered = cleaned.lower()

    phrase = "remember that"
    if phrase not in lowered:
        return None

    idx = lowered.find(phrase)
    fact = cleaned[idx + len(phrase):].strip()

    if not fact:
        return "I can remember something, but you have to tell me what."

    if fact not in memory["memories"]:
        memory["memories"].append(fact)
        save_memory(memory)

    return f"Got it — I’ll remember that {fact}"


def maybe_forget_memory(text: str, memory: dict) -> str | None:
    cleaned = clean_slack_text(text)
    lowered = cleaned.lower()

    phrase = "forget that"
    if phrase not in lowered:
        return None

    idx = lowered.find(phrase)
    target = cleaned[idx + len(phrase):].strip().lower()

    if not target:
        return "Tell me what to forget."

    old_memories = memory["memories"]
    new_memories = [m for m in old_memories if target not in m.lower()]

    if len(new_memories) == len(old_memories):
        return "I couldn’t find that exact memory."

    memory["memories"] = new_memories
    save_memory(memory)

    return "Forgot it."


def generate_reply(user_text: str, memory: dict) -> str:
    cleaned_text = clean_slack_text(user_text)

    memories = memory.get("memories", [])
    memory_text = "\n".join(f"- {m}" for m in memories) if memories else "No memories yet."

    prompt = f"""
{BOT_PERSONALITY}

Memory:
{memory_text}

Latest Slack message:
{cleaned_text}

Reply in Slack style. Keep it short.
"""

    return call_ollama(prompt)



@app.event("app_mention")
def handle_mention(event, say):
    memory = load_memory()

    thread_key, thread_ts = get_thread_key(event)

    if thread_key not in memory["active_threads"]:
        memory["active_threads"].append(thread_key)
        save_memory(memory)

    text = event.get("text", "")

    forget_reply = maybe_forget_memory(text, memory)
    if forget_reply:
        say(text=forget_reply, thread_ts=thread_ts)
        return

    memory_reply = maybe_update_memory(text, memory)
    if memory_reply:
        say(text=memory_reply, thread_ts=thread_ts)
        return

    reply = generate_reply(text, memory)
    say(text=reply, thread_ts=thread_ts)


@app.event("message")
def handle_thread_message(event, say):
    if event.get("bot_id"):
        return

    if "thread_ts" not in event:
        return

    memory = load_memory()
    thread_key, thread_ts = get_thread_key(event)

    if thread_key not in memory.get("active_threads", []):
        return

    text = event.get("text", "")

    forget_reply = maybe_forget_memory(text, memory)
    if forget_reply:
        say(text=forget_reply, thread_ts=thread_ts)
        return

    memory_reply = maybe_update_memory(text, memory)
    if memory_reply:
        say(text=memory_reply, thread_ts=thread_ts)
        return

    reply = generate_reply(text, memory)
    say(text=reply, thread_ts=thread_ts)


if __name__ == "__main__":
    print("ChismeBot is running...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()