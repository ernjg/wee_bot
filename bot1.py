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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

if not SLACK_BOT_TOKEN:
    raise RuntimeError("Missing SLACK_BOT_TOKEN in .env")

if not SLACK_APP_TOKEN:
    raise RuntimeError("Missing SLACK_APP_TOKEN in .env")

BOT_PERSONALITY = Path("personality.txt").read_text(encoding="utf-8")

app = App(token=SLACK_BOT_TOKEN)


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return {"memories": [], "active_threads": []}

    with MEMORY_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(memory: dict) -> None:
    with MEMORY_PATH.open("w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)


def get_thread_ts(event: dict) -> str:
    return event.get("thread_ts") or event["ts"]


def get_thread_key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def clean_slack_text(text: str) -> str:
    text = re.sub(r"<@[^>]+>", "", text)
    text = re.sub(r"<#([^|>]+)\|?([^>]*)>", r"#\2", text)
    text = re.sub(r"<(https?://[^|>]+)\|?([^>]*)>", r"\2", text)
    return text.strip()


def call_ollama(prompt: str) -> str:
    try:
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

    except requests.exceptions.ConnectionError:
        return "I’m alive in Slack, but Ollama is not running locally."

    except requests.exceptions.Timeout:
        return "Ollama took too long to respond."

    except requests.exceptions.HTTPError as e:
        return f"Ollama returned an error: {e}"


def format_slack_messages(messages: list[dict]) -> str:
    lines = []

    for msg in messages:
        user = msg.get("user") or msg.get("bot_id") or "unknown"
        text = clean_slack_text(msg.get("text", ""))

        if not text:
            continue

        lines.append(f"{user}: {text}")

    return "\n".join(lines)


def fetch_thread_context(client, channel: str, thread_ts: str, limit: int = 20) -> str:
    try:
        result = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=limit,
        )

        messages = result.get("messages", [])
        return format_slack_messages(messages)

    except Exception as e:
        print(f"Could not fetch thread context: {e}")
        return ""


def fetch_channel_context(client, channel: str, latest_ts: str, limit: int = 15) -> str:
    try:
        result = client.conversations_history(
            channel=channel,
            latest=latest_ts,
            limit=limit,
            inclusive=False,
        )

        messages = result.get("messages", [])
        messages = list(reversed(messages))
        return format_slack_messages(messages)

    except Exception as e:
        print(f"Could not fetch channel context: {e}")
        return ""


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


def generate_reply(
    user_text: str,
    memory: dict,
    thread_context: str,
    channel_context: str,
) -> str:
    cleaned_text = clean_slack_text(user_text)

    memories = memory.get("memories", [])
    memory_text = "\n".join(f"- {m}" for m in memories) if memories else "No memories yet."

    if not thread_context:
        thread_context = "No thread context available."

    if not channel_context:
        channel_context = "No channel context available."

    prompt = f"""
{BOT_PERSONALITY}

Memory:
{memory_text}

Recent channel context:
{channel_context}

Thread context:
{thread_context}

Latest message:
{cleaned_text}

Write a short Slack reply. Do not summarize everything unless asked.
"""

    return call_ollama(prompt)


@app.event("app_mention")
def handle_mention(event, say, client):
    print("APP MENTION RECEIVED")
    print(event)

    memory = load_memory()

    channel = event["channel"]
    thread_ts = get_thread_ts(event)
    thread_key = get_thread_key(channel, thread_ts)

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

    thread_context = fetch_thread_context(client, channel, thread_ts)
    channel_context = fetch_channel_context(client, channel, event["ts"])

    reply = generate_reply(
        user_text=text,
        memory=memory,
        thread_context=thread_context,
        channel_context=channel_context,
    )

    say(text=reply, thread_ts=thread_ts)


@app.event("message")
def handle_thread_message(event, say, client):
    print("MESSAGE EVENT RECEIVED")
    print(event)

    if event.get("bot_id"):
        return

    subtype = event.get("subtype")
    if subtype is not None:
        return

    if "thread_ts" not in event:
        return

    memory = load_memory()

    channel = event["channel"]
    thread_ts = event["thread_ts"]
    thread_key = get_thread_key(channel, thread_ts)

    print(f"Thread key: {thread_key}")
    print(f"Active threads: {memory.get('active_threads', [])}")

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

    thread_context = fetch_thread_context(client, channel, thread_ts)
    channel_context = fetch_channel_context(client, channel, event["ts"])

    reply = generate_reply(
        user_text=text,
        memory=memory,
        thread_context=thread_context,
        channel_context=channel_context,
    )

    say(text=reply, thread_ts=thread_ts)


if __name__ == "__main__":
    print("ChismeBot is running...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()