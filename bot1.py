import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

# ---------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------

MEMORY_PATH = Path("memory.json")
PERSONALITY_PATH = Path("personality.txt")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

DEBUG = os.getenv("DEBUG", "1") == "1"

ACTIVE_THREAD_TTL_SECONDS = int(os.getenv("ACTIVE_THREAD_TTL_SECONDS", str(60 * 60 * 12)))

THREAD_FETCH_LIMIT = int(os.getenv("THREAD_FETCH_LIMIT", "40"))
CHANNEL_FETCH_LIMIT = int(os.getenv("CHANNEL_FETCH_LIMIT", "30"))

MAX_THREAD_LINES = int(os.getenv("MAX_THREAD_LINES", "12"))
MAX_CHANNEL_LINES = int(os.getenv("MAX_CHANNEL_LINES", "6"))
MAX_MEMORY_LINES = int(os.getenv("MAX_MEMORY_LINES", "8"))

ALWAYS_KEEP_RECENT_THREAD = int(os.getenv("ALWAYS_KEEP_RECENT_THREAD", "5"))
ALWAYS_KEEP_RECENT_CHANNEL = int(os.getenv("ALWAYS_KEEP_RECENT_CHANNEL", "2"))

OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "90"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "120"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.7"))

WEE_NAME_TRIGGERS = {
    "wee",
    "wee marquez",
    "marquez",
}

WEE_ABOUT_PHRASES = {
    "wee",
    "wee marquez",
    "marquez",
    "he crashed",
    "he broke",
    "he is broken",
    "he's broken",
    "hes broken",
    "he isnt responding",
    "he isn't responding",
    "he stopped responding",
    "he is not responding",
    "he's not responding",
    "hes not responding",
    "why isn't he",
    "why isnt he",
    "is he working",
    "he's working",
    "hes working",
    "his memory",
    "his memories",
    "his response",
    "his replies",
    "his personality",
    "wee's memory",
    "wee’s memory",
    "wee's response",
    "wee’s response",
}

WEE_PRONOUN_ABOUT_PHRASES = {
    "he",
    "him",
    "his",
}

if not SLACK_BOT_TOKEN:
    raise RuntimeError("Missing SLACK_BOT_TOKEN in .env")

if not SLACK_APP_TOKEN:
    raise RuntimeError("Missing SLACK_APP_TOKEN in .env")

if not PERSONALITY_PATH.exists():
    raise RuntimeError("Missing personality.txt")

WEE_PERSONALITY = PERSONALITY_PATH.read_text(encoding="utf-8").strip()

app = App(token=SLACK_BOT_TOKEN)

SEEN_EVENT_TS: set[str] = set()
USER_CACHE: dict[str, str] = {}

DEFAULT_MEMORY = {
    "memories": [],
    "active_threads": {}
}


# ---------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------

def debug(message: str) -> None:
    if DEBUG:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}", flush=True)


def debug_event(prefix: str, event: dict[str, Any]) -> None:
    if not DEBUG:
        return

    debug(
        f"{prefix}: "
        f"type={event.get('type')} "
        f"subtype={event.get('subtype')} "
        f"user={event.get('user')} "
        f"channel={event.get('channel')} "
        f"ts={event.get('ts')} "
        f"event_ts={event.get('event_ts')} "
        f"thread_ts={event.get('thread_ts')} "
        f"text={event.get('text', '')!r}"
    )


def now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------

def load_memory() -> dict[str, Any]:
    debug("Loading memory.json")

    if not MEMORY_PATH.exists():
        debug("memory.json does not exist; creating fresh memory")
        save_memory(DEFAULT_MEMORY.copy())
        return DEFAULT_MEMORY.copy()

    try:
        with MEMORY_PATH.open("r", encoding="utf-8") as f:
            memory = json.load(f)
    except json.JSONDecodeError as e:
        broken_path = MEMORY_PATH.with_suffix(".broken.json")
        MEMORY_PATH.replace(broken_path)
        debug(f"memory.json was invalid; moved to {broken_path}")
        debug(f"JSON error: line={e.lineno}, column={e.colno}, msg={e.msg}")
        save_memory(DEFAULT_MEMORY.copy())
        return DEFAULT_MEMORY.copy()

    if "memories" not in memory or not isinstance(memory["memories"], list):
        debug("memory['memories'] missing or invalid; resetting to []")
        memory["memories"] = []

    if "active_threads" not in memory:
        debug("memory['active_threads'] missing; resetting to {}")
        memory["active_threads"] = {}

    if isinstance(memory["active_threads"], list):
        debug("Converting old active_threads list format to dict format")
        memory["active_threads"] = {
            key: {
                "created_at": now(),
                "last_seen": now(),
            }
            for key in memory["active_threads"]
        }

    if not isinstance(memory["active_threads"], dict):
        debug("memory['active_threads'] invalid; resetting to {}")
        memory["active_threads"] = {}

    debug(
        f"Memory loaded: "
        f"{len(memory.get('memories', []))} memories, "
        f"{len(memory.get('active_threads', {}))} active threads"
    )

    return memory


def save_memory(memory: dict[str, Any]) -> None:
    tmp_path = MEMORY_PATH.with_suffix(".tmp.json")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)
    tmp_path.replace(MEMORY_PATH)

    debug(
        f"Memory saved: "
        f"{len(memory.get('memories', []))} memories, "
        f"{len(memory.get('active_threads', {}))} active threads"
    )


def prune_active_threads(memory: dict[str, Any]) -> None:
    cutoff = now() - ACTIVE_THREAD_TTL_SECONDS
    active_threads = memory.get("active_threads", {})
    before = len(active_threads)

    memory["active_threads"] = {
        key: value
        for key, value in active_threads.items()
        if value.get("last_seen", 0) >= cutoff
    }

    after = len(memory["active_threads"])

    if before != after:
        debug(f"Pruned active threads: before={before}, after={after}")


def mark_thread_active(memory: dict[str, Any], channel: str, thread_ts: str) -> None:
    thread_key = get_thread_key(channel, thread_ts)
    active_threads = memory.setdefault("active_threads", {})

    if thread_key not in active_threads:
        debug(f"Marking new active thread: {thread_key}")
        active_threads[thread_key] = {
            "created_at": now(),
            "last_seen": now(),
        }
    else:
        debug(f"Updating active thread last_seen: {thread_key}")
        active_threads[thread_key]["last_seen"] = now()

    prune_active_threads(memory)
    save_memory(memory)


def is_thread_active(memory: dict[str, Any], channel: str, thread_ts: str) -> bool:
    prune_active_threads(memory)

    thread_key = get_thread_key(channel, thread_ts)
    active_threads = memory.get("active_threads", {})

    if thread_key not in active_threads:
        debug(f"Thread is NOT active: {thread_key}")
        return False

    debug(f"Thread is active: {thread_key}")
    active_threads[thread_key]["last_seen"] = now()
    save_memory(memory)
    return True


# ---------------------------------------------------------------------
# Slack text / user formatting
# ---------------------------------------------------------------------

def get_thread_ts(event: dict[str, Any]) -> str:
    return event.get("thread_ts") or event["ts"]


def get_thread_key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def clean_slack_text(text: str) -> str:
    text = re.sub(r"<@([^>]+)>", r"@\1", text)
    text = re.sub(r"<#([^|>]+)\|?([^>]*)>", lambda m: f"#{m.group(2) or m.group(1)}", text)
    text = re.sub(r"<(https?://[^|>]+)\|?([^>]*)>", lambda m: m.group(2) or m.group(1), text)
    return text.strip()


def normalized_text(text: str) -> str:
    cleaned = clean_slack_text(text).lower()
    cleaned = cleaned.replace("’", "'")
    return cleaned


def phrase_in_text(phrase: str, text: str) -> bool:
    pattern = r"(^|[^a-zA-Z0-9_])" + re.escape(phrase.lower()) + r"([^a-zA-Z0-9_]|$)"
    return re.search(pattern, text.lower()) is not None


def text_mentions_wee(text: str) -> bool:
    cleaned = normalized_text(text)

    for trigger in WEE_NAME_TRIGGERS:
        if phrase_in_text(trigger, cleaned):
            debug(f"Text mentions Wee via trigger: {trigger!r}")
            return True

    debug("Text does not mention Wee by name")
    return False


def text_is_about_wee(text: str, active_thread: bool = False) -> bool:
    cleaned = normalized_text(text)

    for phrase in WEE_ABOUT_PHRASES:
        if phrase in cleaned:
            debug(f"Text seems about Wee via phrase: {phrase!r}")
            return True

    if active_thread:
        for phrase in WEE_PRONOUN_ABOUT_PHRASES:
            if phrase_in_text(phrase, cleaned):
                debug(f"Text seems about Wee via active-thread pronoun: {phrase!r}")
                return True

    debug("Text does not seem about Wee")
    return False


def text_asks_wee_question(text: str) -> bool:
    cleaned = normalized_text(text)

    patterns = [
        r"\bwee\b.*\?",
        r"\bwee marquez\b.*\?",
        r"\bmarquez\b.*\?",
        r"\bwee\b\s+(what|why|how|can|could|would|should|do|does|did|are|is)\b",
        r"\bwee marquez\b\s+(what|why|how|can|could|would|should|do|does|did|are|is)\b",
        r"^(what|why|how|can|could|would|should|do|does|did|are|is)\b.*\bwee\b",
    ]

    for pattern in patterns:
        if re.search(pattern, cleaned):
            debug(f"Text asks Wee a question via pattern: {pattern}")
            return True

    debug("Text does not ask Wee a question")
    return False


def should_wee_respond_to_thread_reply(text: str, active_thread: bool) -> bool:
    debug(f"Evaluating thread reply response policy: active_thread={active_thread}")

    if text_mentions_wee(text):
        debug("Decision: respond to thread reply because Wee is named")
        return True

    if text_asks_wee_question(text):
        debug("Decision: respond to thread reply because it asks Wee a question")
        return True

    if text_is_about_wee(text, active_thread=active_thread):
        debug("Decision: respond to thread reply because it seems about Wee")
        return True

    debug("Decision: ignore thread reply")
    return False


def should_wee_respond_to_channel_message(text: str) -> bool:
    debug("Evaluating channel message response policy")

    if text_mentions_wee(text):
        debug("Decision: respond to channel message because Wee is named")
        return True

    if text_asks_wee_question(text):
        debug("Decision: respond to channel message because it asks Wee a question")
        return True

    debug("Decision: ignore channel message")
    return False


def get_user_name(client, user_id: str | None) -> str:
    if not user_id:
        return "unknown"

    if user_id in USER_CACHE:
        return USER_CACHE[user_id]

    try:
        debug(f"Fetching Slack user info for {user_id}")
        result = client.users_info(user=user_id)
        user = result.get("user", {})
        profile = user.get("profile", {})

        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("name")
            or user_id
        )

        USER_CACHE[user_id] = name
        debug(f"Resolved user {user_id} -> {name}")
        return name

    except Exception as e:
        debug(f"Could not fetch user info for {user_id}: {e}")
        return user_id


def slack_messages_to_lines(client, messages: list[dict[str, Any]]) -> list[str]:
    lines = []

    for msg in messages:
        text = clean_slack_text(msg.get("text", ""))
        if not text:
            continue

        user_id = msg.get("user")
        app_id = msg.get("app_id")
        bot_id = msg.get("bot_id")

        if user_id:
            speaker = get_user_name(client, user_id)
        elif app_id or bot_id:
            speaker = "Wee Marquez"
        else:
            speaker = "unknown"

        lines.append(f"{speaker}: {text}")

    return lines


def format_lines(lines: list[str]) -> str:
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Cheap relevance filtering
# ---------------------------------------------------------------------

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "is", "are", "was",
    "were", "to", "of", "in", "on", "for", "with", "as", "at", "by", "from",
    "it", "this", "that", "these", "those", "i", "you", "he", "she", "we",
    "they", "me", "my", "your", "our", "their", "what", "how", "why", "can",
    "could", "would", "should", "do", "does", "did", "be", "been", "being",
    "not", "no", "yes", "just", "like", "really", "very", "also", "there",
    "here", "about", "into", "out", "up", "down", "so", "because", "than",
}


def tokenize(text: str) -> set[str]:
    text = normalized_text(text)
    words = re.findall(r"[a-zA-Z0-9_']+", text)
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def relevance_score(query: str, text: str) -> int:
    query_terms = tokenize(query)
    text_terms = tokenize(text)

    if not query_terms or not text_terms:
        return 0

    score = len(query_terms & text_terms)

    cleaned_query = normalized_text(query)
    cleaned_text = normalized_text(text)

    for term in query_terms:
        if term in cleaned_text:
            score += 1

    if cleaned_query and cleaned_query in cleaned_text:
        score += 5

    return score


def select_relevant_items(
    query: str,
    items: list[str],
    max_items: int,
    always_keep_last: int = 0,
) -> list[str]:
    if not items:
        debug("Relevant selection: no items available")
        return []

    recent = items[-always_keep_last:] if always_keep_last > 0 else []
    older = items[:-always_keep_last] if always_keep_last > 0 else items

    scored = [
        (relevance_score(query, item), i, item)
        for i, item in enumerate(older)
    ]

    relevant = [
        item
        for score, _, item in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)
        if score > 0
    ]

    selected = relevant[: max(0, max_items - len(recent))] + recent

    seen = set()
    result = []
    for item in selected:
        if item not in seen:
            seen.add(item)
            result.append(item)

    result = result[-max_items:]

    debug(
        f"Relevant selection: total={len(items)}, "
        f"older={len(older)}, recent_kept={len(recent)}, "
        f"scored_relevant={len(relevant)}, selected={len(result)}"
    )

    return result


# ---------------------------------------------------------------------
# Slack context fetching
# ---------------------------------------------------------------------

def fetch_thread_context(
    client,
    channel: str,
    thread_ts: str,
    limit: int = THREAD_FETCH_LIMIT,
) -> list[str]:
    start = time.time()

    try:
        debug(f"Fetching thread context: channel={channel}, thread_ts={thread_ts}, limit={limit}")
        result = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=limit,
        )

        messages = result.get("messages", [])
        lines = slack_messages_to_lines(client, messages)

        debug(
            f"Fetched thread context: messages={len(messages)}, "
            f"lines={len(lines)}, seconds={time.time() - start:.2f}"
        )

        return lines

    except Exception as e:
        debug(f"Could not fetch thread context: {e}")
        return []


def fetch_channel_context(
    client,
    channel: str,
    latest_ts: str,
    limit: int = CHANNEL_FETCH_LIMIT,
) -> list[str]:
    start = time.time()

    try:
        debug(f"Fetching channel context: channel={channel}, latest_ts={latest_ts}, limit={limit}")
        result = client.conversations_history(
            channel=channel,
            latest=latest_ts,
            limit=limit,
            inclusive=False,
        )

        messages = result.get("messages", [])
        messages = list(reversed(messages))
        lines = slack_messages_to_lines(client, messages)

        debug(
            f"Fetched channel context: messages={len(messages)}, "
            f"lines={len(lines)}, seconds={time.time() - start:.2f}"
        )

        return lines

    except Exception as e:
        debug(f"Could not fetch channel context: {e}")
        return []


# ---------------------------------------------------------------------
# Memory commands
# ---------------------------------------------------------------------

def maybe_update_memory(text: str, memory: dict[str, Any]) -> str | None:
    cleaned = clean_slack_text(text)
    lowered = normalized_text(cleaned)

    phrase = "remember that"
    if phrase not in lowered:
        return None

    idx = lowered.find(phrase)
    fact = cleaned[idx + len(phrase):].strip()

    if not fact:
        debug("Memory command found, but no fact was provided")
        return "I can remember something, but you have to tell me what."

    if fact not in memory["memories"]:
        debug(f"Adding memory: {fact!r}")
        memory["memories"].append(fact)
        save_memory(memory)
    else:
        debug(f"Memory already exists: {fact!r}")

    return f"Got it — I’ll remember that {fact}"


def maybe_forget_memory(text: str, memory: dict[str, Any]) -> str | None:
    cleaned = clean_slack_text(text)
    lowered = normalized_text(cleaned)

    phrase = "forget that"
    if phrase not in lowered:
        return None

    idx = lowered.find(phrase)
    target = cleaned[idx + len(phrase):].strip().lower()

    if not target:
        debug("Forget command found, but no target was provided")
        return "Tell me what to forget."

    old_memories = memory["memories"]
    new_memories = [m for m in old_memories if target not in m.lower()]

    if len(new_memories) == len(old_memories):
        debug(f"Forget command found no matching memory for target: {target!r}")
        return "I couldn’t find that exact memory."

    debug(f"Forgot memories matching target: {target!r}")
    memory["memories"] = new_memories
    save_memory(memory)

    return "Forgot it."


def maybe_show_memory(text: str, memory: dict[str, Any]) -> str | None:
    cleaned = normalized_text(text)

    triggers = [
        "what do you remember",
        "show memory",
        "show memories",
        "what are your memories",
        "what does wee remember",
        "what do you know",
    ]

    if not any(t in cleaned for t in triggers):
        return None

    memories = memory.get("memories", [])
    debug(f"Show memory command found: memories={len(memories)}")

    if not memories:
        return "I don’t remember anything yet."

    lines = "\n".join(f"• {m}" for m in memories[-20:])
    return f"Here’s what I remember:\n{lines}"


def handle_memory_commands(text: str, memory: dict[str, Any]) -> str | None:
    debug("Checking memory commands")

    reply = (
        maybe_show_memory(text, memory)
        or maybe_forget_memory(text, memory)
        or maybe_update_memory(text, memory)
    )

    if reply:
        debug(f"Memory command matched; reply={reply!r}")
    else:
        debug("No memory command matched")

    return reply


# ---------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------

def call_ollama(prompt: str) -> str:
    start = time.time()

    debug(
        f"Calling Ollama: model={OLLAMA_MODEL}, "
        f"prompt_chars={len(prompt)}, "
        f"num_predict={OLLAMA_NUM_PREDICT}, "
        f"num_ctx={OLLAMA_NUM_CTX}"
    )

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": OLLAMA_NUM_PREDICT,
                    "temperature": OLLAMA_TEMPERATURE,
                    "num_ctx": OLLAMA_NUM_CTX,
                },
            },
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )

        response.raise_for_status()
        reply = response.json()["response"].strip()

        debug(
            f"Ollama replied: reply_chars={len(reply)}, "
            f"seconds={time.time() - start:.2f}"
        )

        return reply

    except requests.exceptions.ConnectionError:
        debug("Ollama connection error")
        return "I’m here, but my local Ollama brain is not running."

    except requests.exceptions.Timeout:
        debug(f"Ollama timeout after {OLLAMA_TIMEOUT_SECONDS} seconds")
        return "Ollama took too long to respond. My local brain is sweating."

    except requests.exceptions.HTTPError as e:
        debug(f"Ollama HTTP error: {e}")
        return f"Ollama returned an error: {e}"


# ---------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------

def build_prompt(
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
) -> str:
    cleaned_latest = clean_slack_text(latest_message)

    debug("Building prompt")

    relevant_thread = select_relevant_items(
        query=cleaned_latest,
        items=thread_context_lines,
        max_items=MAX_THREAD_LINES,
        always_keep_last=ALWAYS_KEEP_RECENT_THREAD,
    )

    relevant_channel = select_relevant_items(
        query=cleaned_latest,
        items=channel_context_lines,
        max_items=MAX_CHANNEL_LINES,
        always_keep_last=ALWAYS_KEEP_RECENT_CHANNEL,
    )

    relevant_memories = select_relevant_items(
        query=cleaned_latest,
        items=memory.get("memories", []),
        max_items=MAX_MEMORY_LINES,
        always_keep_last=2,
    )

    memory_text = (
        "\n".join(f"- {m}" for m in relevant_memories)
        if relevant_memories
        else "No relevant memories."
    )

    thread_text = (
        format_lines(relevant_thread)
        if relevant_thread
        else "No relevant thread context."
    )

    channel_text = (
        format_lines(relevant_channel)
        if relevant_channel
        else "No relevant channel context."
    )

    debug(
        f"Prompt context selected: "
        f"memories={len(relevant_memories)}, "
        f"thread_lines={len(relevant_thread)}, "
        f"channel_lines={len(relevant_channel)}"
    )

    prompt = f"""
{WEE_PERSONALITY}

Relevant memory:
{memory_text}

Relevant channel context:
{channel_text}

Relevant thread context:
{thread_text}

Latest message:
{speaker_name}: {cleaned_latest}

Instructions:
- You are Wee Marquez.
- Reply to the latest message.
- Prefer thread context over channel context.
- Use channel context only if it is clearly helpful.
- Do not summarize all context unless asked.
- Keep the reply short and Slack-like.
""".strip()

    debug(f"Prompt built: chars={len(prompt)}")

    return prompt


def generate_reply(
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
) -> str:
    prompt = build_prompt(
        latest_message=latest_message,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
    )

    return call_ollama(prompt)


# ---------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------

def should_ignore_event(event: dict[str, Any]) -> bool:
    if event.get("bot_id"):
        debug("Ignoring event: message came from an app/automation")
        return True

    if event.get("subtype") is not None:
        debug(f"Ignoring event: subtype={event.get('subtype')}")
        return True

    event_ts = event.get("event_ts") or event.get("ts")
    if event_ts in SEEN_EVENT_TS:
        debug(f"Ignoring event: duplicate event_ts={event_ts}")
        return True

    if event_ts:
        SEEN_EVENT_TS.add(event_ts)

    if len(SEEN_EVENT_TS) > 1000:
        debug("Clearing SEEN_EVENT_TS cache")
        SEEN_EVENT_TS.clear()

    return False


def respond_as_wee(
    *,
    event: dict[str, Any],
    say,
    client,
    memory: dict[str, Any],
    channel: str,
    thread_ts: str,
    include_channel_context: bool,
) -> None:
    start = time.time()

    text = event.get("text", "")
    speaker_name = get_user_name(client, event.get("user"))

    debug(
        f"Preparing response: "
        f"speaker={speaker_name}, channel={channel}, thread_ts={thread_ts}, "
        f"include_channel_context={include_channel_context}, text={text!r}"
    )

    command_reply = handle_memory_commands(text, memory)
    if command_reply:
        debug("Sending memory-command reply")
        say(text=command_reply, thread_ts=thread_ts)
        debug(f"Memory-command reply sent in {time.time() - start:.2f}s")
        return

    thread_context_lines = fetch_thread_context(
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        limit=THREAD_FETCH_LIMIT,
    )

    if include_channel_context:
        channel_context_lines = fetch_channel_context(
            client=client,
            channel=channel,
            latest_ts=event["ts"],
            limit=CHANNEL_FETCH_LIMIT,
        )
    else:
        channel_context_lines = []
        debug("Skipping channel context for this response")

    reply = generate_reply(
        latest_message=text,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
    )

    debug(f"Sending Slack reply: {reply!r}")
    say(text=reply, thread_ts=thread_ts)
    debug(f"Slack reply sent. Total response time={time.time() - start:.2f}s")


# ---------------------------------------------------------------------
# Slack handlers
# ---------------------------------------------------------------------

@app.event("app_mention")
def handle_explicit_mention(event, say, client):
    debug_event("APP_MENTION RECEIVED", event)

    if should_ignore_event(event):
        return

    debug("Route: explicit Slack mention")

    memory = load_memory()

    channel = event["channel"]
    thread_ts = get_thread_ts(event)

    mark_thread_active(memory, channel, thread_ts)

    respond_as_wee(
        event=event,
        say=say,
        client=client,
        memory=memory,
        channel=channel,
        thread_ts=thread_ts,
        include_channel_context=True,
    )


@app.event("message")
def handle_message(event, say, client):
    debug_event("MESSAGE RECEIVED", event)

    if should_ignore_event(event):
        return

    text = event.get("text", "")
    channel = event["channel"]
    memory = load_memory()

    is_thread_reply = "thread_ts" in event
    debug(f"Message classification: is_thread_reply={is_thread_reply}")

    if not is_thread_reply:
        debug("Route: normal channel message")

        if not should_wee_respond_to_channel_message(text):
            debug("Final decision: ignore normal channel message")
            return

        debug("Final decision: respond to normal channel message")

        thread_ts = event["ts"]
        mark_thread_active(memory, channel, thread_ts)

        respond_as_wee(
            event=event,
            say=say,
            client=client,
            memory=memory,
            channel=channel,
            thread_ts=thread_ts,
            include_channel_context=True,
        )
        return

    debug("Route: thread reply")

    thread_ts = event["thread_ts"]
    active_thread = is_thread_active(memory, channel, thread_ts)

    should_respond = should_wee_respond_to_thread_reply(
        text,
        active_thread=active_thread,
    )

    debug(
        f"Thread response decision: "
        f"active_thread={active_thread}, should_respond={should_respond}"
    )

    if not should_respond:
        debug("Final decision: ignore thread reply")
        return

    debug("Final decision: respond to thread reply")

    mark_thread_active(memory, channel, thread_ts)

    respond_as_wee(
        event=event,
        say=say,
        client=client,
        memory=memory,
        channel=channel,
        thread_ts=thread_ts,
        include_channel_context=False,
    )


if __name__ == "__main__":
    print("Wee Marquez is running...")
    print(f"Using Ollama model: {OLLAMA_MODEL}")
    print(f"Debug logging: {DEBUG}")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()