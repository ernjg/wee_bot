import argparse
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()


DEFAULT_MEMORY = {
    "version": 2,
    "memories": [],
    "active_threads": {},
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "is", "are", "was",
    "were", "to", "of", "in", "on", "for", "with", "as", "at", "by", "from",
    "it", "this", "that", "these", "those", "i", "you", "he", "she", "we",
    "they", "me", "my", "your", "our", "their", "what", "how", "why", "can",
    "could", "would", "should", "do", "does", "did", "be", "been", "being",
    "not", "no", "yes", "just", "like", "really", "very", "also", "there",
    "here", "about", "into", "out", "up", "down", "so", "because", "than",
}


@dataclass(frozen=True)
class AgentConfig:
    name: str
    display_name: str
    root: Path
    memory_path: Path
    personality_path: Path
    slack_bot_token: str
    slack_app_token: str
    ollama_model: str
    bot_user_id: str | None
    triggers: tuple[str, ...]
    about_phrases: tuple[str, ...]
    pronoun_about_phrases: tuple[str, ...]
    debug: bool
    allow_bot_messages: bool
    active_thread_ttl_seconds: int
    max_active_threads: int
    thread_fetch_limit: int
    channel_fetch_limit: int
    max_thread_lines: int
    max_channel_lines: int
    max_memory_lines: int
    always_keep_recent_thread: int
    always_keep_recent_channel: int
    ollama_timeout_seconds: int
    ollama_num_predict: int
    ollama_num_ctx: int
    ollama_temperature: float
    response_cooldown_seconds: int
    max_auto_replies_per_thread: int
    max_memories: int
    low_confidence_memory_ttl_days: int


class SeenEventCache:
    def __init__(self, limit: int = 1000) -> None:
        self.limit = limit
        self.items: OrderedDict[str, int] = OrderedDict()

    def add_or_seen(self, key: str | None) -> bool:
        if not key:
            return False
        if key in self.items:
            return True
        self.items[key] = now()
        while len(self.items) > self.limit:
            self.items.popitem(last=False)
        return False


USER_CACHE: dict[str, str] = {}
SEEN_EVENTS = SeenEventCache()


def now() -> int:
    return int(time.time())


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def agent_env_prefix(agent_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", agent_name).upper()


def get_agent_env(agent_name: str, key: str, default: str | None = None) -> str | None:
    agent_value = os.getenv(f"{agent_env_prefix(agent_name)}_{key}")
    if agent_value is not None:
        return agent_value
    return os.getenv(key, default)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return data


def load_agent_config(agent_name: str) -> AgentConfig:
    agent_root = Path("agents") / agent_name
    config_path = agent_root / "config.json"
    if not config_path.exists():
        raise RuntimeError(f"Missing agent config: {config_path}")
    raw = read_json(config_path)

    display_name = raw.get("display_name") or agent_name
    bot_user_id = raw.get("bot_user_id") or get_agent_env(agent_name, "SLACK_BOT_USER_ID")

    triggers = raw.get("triggers") or [agent_name, display_name]
    if bot_user_id:
        triggers.append(f"<@{bot_user_id}>")
        triggers.append(bot_user_id)

    about_phrases = raw.get("about_phrases") or [
        agent_name,
        display_name,
        "you",
        "your memory",
        "your memories",
        "your response",
        "your replies",
        "your personality",
        "are you working",
        "why are you not responding",
        "why aren't you responding",
    ]

    slack_bot_token_env = raw.get("slack_bot_token_env")
    slack_app_token_env = raw.get("slack_app_token_env")
    slack_bot_token = (
        os.getenv(slack_bot_token_env) if slack_bot_token_env else None
    ) or get_agent_env(agent_name, "SLACK_BOT_TOKEN")
    slack_app_token = (
        os.getenv(slack_app_token_env) if slack_app_token_env else None
    ) or get_agent_env(agent_name, "SLACK_APP_TOKEN")

    if not slack_bot_token:
        raise RuntimeError(
            f"Missing Slack bot token. Set {agent_env_prefix(agent_name)}_SLACK_BOT_TOKEN "
            "or SLACK_BOT_TOKEN."
        )
    if not slack_app_token:
        raise RuntimeError(
            f"Missing Slack app token. Set {agent_env_prefix(agent_name)}_SLACK_APP_TOKEN "
            "or SLACK_APP_TOKEN."
        )

    personality_path = agent_root / raw.get("personality_file", "personality.txt")
    memory_path = agent_root / raw.get("memory_file", "memory.json")
    if not personality_path.exists():
        raise RuntimeError(f"Missing personality file: {personality_path}")

    return AgentConfig(
        name=agent_name,
        display_name=display_name,
        root=agent_root,
        memory_path=memory_path,
        personality_path=personality_path,
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        ollama_model=raw.get("ollama_model") or get_agent_env(agent_name, "OLLAMA_MODEL", "llama3.1:8b"),
        bot_user_id=bot_user_id,
        triggers=tuple({normalize_phrase(t) for t in triggers if str(t).strip()}),
        about_phrases=tuple({normalize_phrase(t) for t in about_phrases if str(t).strip()}),
        pronoun_about_phrases=tuple(raw.get("pronoun_about_phrases", ["he", "him", "his", "they", "them"])),
        debug=env_bool("DEBUG", True),
        allow_bot_messages=env_bool("ALLOW_BOT_MESSAGES", False),
        active_thread_ttl_seconds=env_int("ACTIVE_THREAD_TTL_SECONDS", 60 * 60 * 12),
        max_active_threads=env_int("MAX_ACTIVE_THREADS", 100),
        thread_fetch_limit=env_int("THREAD_FETCH_LIMIT", 40),
        channel_fetch_limit=env_int("CHANNEL_FETCH_LIMIT", 30),
        max_thread_lines=env_int("MAX_THREAD_LINES", 12),
        max_channel_lines=env_int("MAX_CHANNEL_LINES", 6),
        max_memory_lines=env_int("MAX_MEMORY_LINES", 8),
        always_keep_recent_thread=env_int("ALWAYS_KEEP_RECENT_THREAD", 5),
        always_keep_recent_channel=env_int("ALWAYS_KEEP_RECENT_CHANNEL", 2),
        ollama_timeout_seconds=env_int("OLLAMA_TIMEOUT_SECONDS", 90),
        ollama_num_predict=env_int("OLLAMA_NUM_PREDICT", 120),
        ollama_num_ctx=env_int("OLLAMA_NUM_CTX", 4096),
        ollama_temperature=env_float("OLLAMA_TEMPERATURE", 0.7),
        response_cooldown_seconds=env_int("RESPONSE_COOLDOWN_SECONDS", 20),
        max_auto_replies_per_thread=env_int("MAX_AUTO_REPLIES_PER_THREAD", 6),
        max_memories=env_int("MAX_MEMORIES", 200),
        low_confidence_memory_ttl_days=env_int("LOW_CONFIDENCE_MEMORY_TTL_DAYS", 30),
    )


def debug(config: AgentConfig, message: str) -> None:
    if config.debug:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] [{config.name}] {message}", flush=True)


def debug_event(config: AgentConfig, prefix: str, event: dict[str, Any]) -> None:
    if not config.debug:
        return
    debug(
        config,
        f"{prefix}: type={event.get('type')} subtype={event.get('subtype')} "
        f"user={event.get('user')} bot_id={event.get('bot_id')} "
        f"channel={event.get('channel')} ts={event.get('ts')} "
        f"event_ts={event.get('event_ts')} thread_ts={event.get('thread_ts')} "
        f"text={event.get('text', '')!r}",
    )


def normalize_phrase(text: str) -> str:
    return clean_slack_text(str(text)).lower().replace("’", "'").strip()


def clean_slack_text(text: str) -> str:
    text = re.sub(r"<@([^>]+)>", r"@\1", text or "")
    text = re.sub(r"<#([^|>]+)\|?([^>]*)>", lambda m: f"#{m.group(2) or m.group(1)}", text)
    text = re.sub(r"<(https?://[^|>]+)\|?([^>]*)>", lambda m: m.group(2) or m.group(1), text)
    return text.strip()


def phrase_in_text(phrase: str, text: str) -> bool:
    phrase = normalize_phrase(phrase)
    text = normalize_phrase(text)
    if not phrase:
        return False
    if phrase.startswith("@"):
        return phrase in text
    pattern = r"(^|[^a-zA-Z0-9_])" + re.escape(phrase) + r"([^a-zA-Z0-9_]|$)"
    return re.search(pattern, text) is not None


def text_mentions_agent(config: AgentConfig, text: str) -> bool:
    return any(phrase_in_text(trigger, text) for trigger in config.triggers)


def text_is_about_agent(config: AgentConfig, text: str, active_thread: bool) -> bool:
    cleaned = normalize_phrase(text)
    if any(phrase in cleaned for phrase in config.about_phrases):
        return True
    return active_thread and any(phrase_in_text(p, cleaned) for p in config.pronoun_about_phrases)


def text_asks_agent_question(config: AgentConfig, text: str) -> bool:
    cleaned = normalize_phrase(text)
    trigger_pattern = "|".join(re.escape(t) for t in config.triggers if not t.startswith("@"))
    if trigger_pattern and re.search(rf"\b({trigger_pattern})\b.*\?", cleaned):
        return True
    if trigger_pattern and re.search(rf"\b(what|why|how|can|could|would|should|do|does|did|are|is)\b.*\b({trigger_pattern})\b", cleaned):
        return True
    return False


def text_asks_active_followup(text: str) -> bool:
    cleaned = normalize_phrase(text)
    if re.search(r"\b(what|why|how|can|could|would|should|do|does|did|are|is)\b.*\byou\b", cleaned):
        return True
    return any(p in cleaned for p in ["what do you think", "thoughts?", "wdyt", "right?", "yes?", "no?"])


def tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9_']+", normalize_phrase(text))
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def relevance_score(query: str, text: str) -> int:
    query_terms = tokenize(query)
    text_terms = tokenize(text)
    if not query_terms or not text_terms:
        return 0
    score = len(query_terms & text_terms)
    cleaned_text = normalize_phrase(text)
    for term in query_terms:
        if term in cleaned_text:
            score += 1
    if normalize_phrase(query) in cleaned_text:
        score += 5
    return score


def select_relevant_items(
    query: str,
    items: list[str],
    max_items: int,
    always_keep_last: int = 0,
) -> list[str]:
    if not items or max_items <= 0:
        return []
    recent = items[-always_keep_last:] if always_keep_last > 0 else []
    older = items[:-always_keep_last] if always_keep_last > 0 else items
    scored = [
        (relevance_score(query, item), i, item)
        for i, item in enumerate(older)
    ]
    relevant = [
        item for score, _, item in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)
        if score > 0
    ]
    selected = relevant[: max(0, max_items - len(recent))] + recent
    deduped = list(dict.fromkeys(selected))
    return deduped[-max_items:]


def memory_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("text", "")).strip()
    return ""


def memory_id(text: str) -> str:
    return hashlib.sha1(normalize_phrase(text).encode("utf-8")).hexdigest()[:12]


def make_memory_item(text: str, source: str, confidence: float) -> dict[str, Any]:
    timestamp = now()
    return {
        "id": memory_id(text),
        "text": text,
        "confidence": round(confidence, 2),
        "source": source,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_used": 0,
        "uses": 0,
    }


def load_memory(config: AgentConfig) -> dict[str, Any]:
    if not config.memory_path.exists():
        memory = DEFAULT_MEMORY.copy()
        save_memory(config, memory)
        return memory

    try:
        with config.memory_path.open("r", encoding="utf-8") as f:
            memory = json.load(f)
    except json.JSONDecodeError as e:
        broken_path = config.memory_path.with_suffix(".broken.json")
        config.memory_path.replace(broken_path)
        debug(config, f"Invalid memory moved to {broken_path}: line={e.lineno} column={e.colno}")
        memory = DEFAULT_MEMORY.copy()
        save_memory(config, memory)
        return memory

    if not isinstance(memory, dict):
        memory = DEFAULT_MEMORY.copy()
    memory.setdefault("version", 2)
    memory.setdefault("memories", [])
    memory.setdefault("active_threads", {})

    if isinstance(memory["active_threads"], list):
        memory["active_threads"] = {
            key: {"created_at": now(), "last_seen": now(), "auto_reply_count": 0}
            for key in memory["active_threads"]
        }
    if not isinstance(memory["active_threads"], dict):
        memory["active_threads"] = {}

    migrated_memories = []
    for item in memory.get("memories", []):
        text = memory_item_text(item)
        if not text:
            continue
        if isinstance(item, dict):
            item.setdefault("id", memory_id(text))
            item.setdefault("confidence", 0.75)
            item.setdefault("source", "memory_file")
            item.setdefault("created_at", now())
            item.setdefault("updated_at", item["created_at"])
            item.setdefault("last_used", 0)
            item.setdefault("uses", 0)
            migrated_memories.append(item)
        else:
            migrated_memories.append(make_memory_item(text, "migrated", 0.75))
    memory["memories"] = migrated_memories

    prune_memory(config, memory)
    return memory


def save_memory(config: AgentConfig, memory: dict[str, Any]) -> None:
    config.memory_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config.memory_path.with_suffix(".tmp.json")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)
    tmp_path.replace(config.memory_path)


def prune_memory(config: AgentConfig, memory: dict[str, Any]) -> None:
    cutoff = now() - (config.low_confidence_memory_ttl_days * 24 * 60 * 60)
    kept = []
    for item in memory.get("memories", []):
        confidence = float(item.get("confidence", 0.75))
        created_at = int(item.get("created_at", now()))
        if confidence < 0.5 and created_at < cutoff:
            continue
        kept.append(item)

    kept.sort(key=lambda m: (float(m.get("confidence", 0)), int(m.get("last_used", 0)), int(m.get("updated_at", 0))), reverse=True)
    memory["memories"] = kept[: config.max_memories]


def prune_active_threads(config: AgentConfig, memory: dict[str, Any]) -> None:
    cutoff = now() - config.active_thread_ttl_seconds
    active_threads = memory.get("active_threads", {})
    kept = {
        key: value for key, value in active_threads.items()
        if int(value.get("last_seen", 0)) >= cutoff
    }
    if len(kept) > config.max_active_threads:
        newest = sorted(kept.items(), key=lambda item: int(item[1].get("last_seen", 0)), reverse=True)
        kept = dict(newest[: config.max_active_threads])
    memory["active_threads"] = kept


def get_thread_ts(event: dict[str, Any]) -> str:
    return event.get("thread_ts") or event["ts"]


def get_thread_key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def mark_thread_active(config: AgentConfig, memory: dict[str, Any], channel: str, thread_ts: str) -> None:
    thread_key = get_thread_key(channel, thread_ts)
    timestamp = now()
    thread = memory.setdefault("active_threads", {}).setdefault(
        thread_key,
        {
            "created_at": timestamp,
            "last_seen": timestamp,
            "last_bot_reply_at": 0,
            "auto_reply_count": 0,
        },
    )
    thread["last_seen"] = timestamp
    prune_active_threads(config, memory)
    save_memory(config, memory)


def get_thread_state(config: AgentConfig, memory: dict[str, Any], channel: str, thread_ts: str) -> dict[str, Any] | None:
    prune_active_threads(config, memory)
    state = memory.get("active_threads", {}).get(get_thread_key(channel, thread_ts))
    if state:
        state["last_seen"] = now()
        save_memory(config, memory)
    return state


def record_bot_reply(config: AgentConfig, memory: dict[str, Any], channel: str, thread_ts: str, explicit: bool) -> None:
    thread_key = get_thread_key(channel, thread_ts)
    thread = memory.setdefault("active_threads", {}).setdefault(
        thread_key,
        {"created_at": now(), "last_seen": now(), "last_bot_reply_at": 0, "auto_reply_count": 0},
    )
    thread["last_seen"] = now()
    thread["last_bot_reply_at"] = now()
    if not explicit:
        thread["auto_reply_count"] = int(thread.get("auto_reply_count", 0)) + 1
    save_memory(config, memory)


def should_ignore_event(config: AgentConfig, event: dict[str, Any]) -> bool:
    if event.get("subtype") is not None and event.get("subtype") != "bot_message":
        debug(config, f"Ignoring event subtype={event.get('subtype')}")
        return True

    if config.bot_user_id and event.get("user") == config.bot_user_id:
        debug(config, "Ignoring own bot user event")
        return True

    if (event.get("bot_id") or event.get("app_id")) and not config.allow_bot_messages:
        debug(config, "Ignoring bot/app message; set ALLOW_BOT_MESSAGES=1 to opt in")
        return True

    event_ts = event.get("event_ts") or event.get("ts")
    if SEEN_EVENTS.add_or_seen(event_ts):
        debug(config, f"Ignoring duplicate event_ts={event_ts}")
        return True
    return False


def get_user_name(config: AgentConfig, client, user_id: str | None) -> str:
    if not user_id:
        return "unknown"
    if user_id in USER_CACHE:
        return USER_CACHE[user_id]
    try:
        result = client.users_info(user=user_id)
        user = result.get("user", {})
        profile = user.get("profile", {})
        name = profile.get("display_name") or profile.get("real_name") or user.get("name") or user_id
        USER_CACHE[user_id] = name
        return name
    except Exception as e:
        debug(config, f"Could not fetch user info for {user_id}: {e}")
        return user_id


def slack_messages_to_lines(config: AgentConfig, client, messages: list[dict[str, Any]]) -> list[str]:
    lines = []
    for msg in messages:
        text = clean_slack_text(msg.get("text", ""))
        if not text:
            continue
        user_id = msg.get("user")
        if user_id:
            speaker = get_user_name(config, client, user_id)
        elif msg.get("app_id") or msg.get("bot_id"):
            speaker = msg.get("username") or "bot"
        else:
            speaker = "unknown"
        lines.append(f"{speaker}: {text}")
    return lines


def fetch_thread_context(config: AgentConfig, client, channel: str, thread_ts: str) -> list[str]:
    try:
        result = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=config.thread_fetch_limit,
        )
        return slack_messages_to_lines(config, client, result.get("messages", []))
    except Exception as e:
        debug(config, f"Could not fetch thread context: {e}")
        return []


def fetch_channel_context(config: AgentConfig, client, channel: str, latest_ts: str) -> list[str]:
    try:
        result = client.conversations_history(
            channel=channel,
            latest=latest_ts,
            limit=config.channel_fetch_limit,
            inclusive=False,
        )
        return slack_messages_to_lines(config, client, list(reversed(result.get("messages", []))))
    except Exception as e:
        debug(config, f"Could not fetch channel context: {e}")
        return []


def find_memory_command_payload(text: str, commands: tuple[str, ...]) -> str | None:
    cleaned = clean_slack_text(text)
    lowered = normalize_phrase(cleaned)
    for command in commands:
        idx = lowered.find(command)
        if idx >= 0:
            return cleaned[idx + len(command):].strip(" :.-")
    return None


def memory_confidence(text: str) -> float:
    lowered = normalize_phrase(text)
    if any(word in lowered for word in ["maybe", "might", "i think", "probably", "rumor", "allegedly"]):
        return 0.45
    if any(word in lowered for word in ["definitely", "always", "confirmed", "for sure"]):
        return 0.9
    return 0.7


def maybe_update_memory(config: AgentConfig, text: str, memory: dict[str, Any], speaker_name: str) -> str | None:
    fact = find_memory_command_payload(text, ("remember that", "remember this", "remember:"))
    if fact is None:
        return None
    if not fact:
        return "I can remember something, but you have to tell me what."

    confidence = memory_confidence(fact)
    item = make_memory_item(fact, speaker_name, confidence)
    memories = memory.setdefault("memories", [])
    existing = next((m for m in memories if m.get("id") == item["id"]), None)
    if existing:
        existing.update({"text": fact, "updated_at": now(), "confidence": max(existing.get("confidence", 0.7), confidence)})
    else:
        memories.append(item)

    prune_memory(config, memory)
    save_memory(config, memory)
    if confidence < 0.5:
        return f"I'll keep that as a low-confidence memory: {fact}"
    return f"Got it. I'll remember {fact}"


def maybe_forget_memory(config: AgentConfig, text: str, memory: dict[str, Any]) -> str | None:
    target = find_memory_command_payload(text, ("forget that", "forget this", "forget:"))
    if target is None:
        return None
    if not target:
        return "Tell me what to forget."
    if normalize_phrase(target) in {"everything", "all", "all memories"}:
        count = len(memory.get("memories", []))
        memory["memories"] = []
        save_memory(config, memory)
        return f"Forgot {count} memories."

    old_memories = memory.get("memories", [])
    normalized_target = normalize_phrase(target)
    new_memories = [
        item for item in old_memories
        if normalized_target not in normalize_phrase(memory_item_text(item))
    ]
    if len(new_memories) == len(old_memories):
        return "I couldn't find a matching memory."
    memory["memories"] = new_memories
    save_memory(config, memory)
    return f"Forgot {len(old_memories) - len(new_memories)} matching memory."


def maybe_show_memory(config: AgentConfig, text: str, memory: dict[str, Any]) -> str | None:
    cleaned = normalize_phrase(text)
    triggers = [
        "what do you remember",
        "show memory",
        "show memories",
        "what are your memories",
        f"what does {normalize_phrase(config.display_name)} remember",
    ]
    if not any(t in cleaned for t in triggers):
        return None
    memories = memory.get("memories", [])
    if not memories:
        return "I don't remember anything yet."
    recent = memories[-20:]
    lines = "\n".join(
        f"- {memory_item_text(item)} (confidence {float(item.get('confidence', 0.7)):.2f})"
        for item in recent
    )
    return f"Here's what I remember:\n{lines}"


def handle_memory_commands(config: AgentConfig, text: str, memory: dict[str, Any], speaker_name: str) -> str | None:
    return (
        maybe_show_memory(config, text, memory)
        or maybe_forget_memory(config, text, memory)
        or maybe_update_memory(config, text, memory, speaker_name)
    )


def selected_memory_lines(config: AgentConfig, latest_message: str, memory: dict[str, Any]) -> list[str]:
    memories = [
        item for item in memory.get("memories", [])
        if float(item.get("confidence", 0.7)) >= 0.4
    ]
    by_relevance = select_relevant_items(
        latest_message,
        [memory_item_text(item) for item in memories],
        config.max_memory_lines,
        always_keep_last=2,
    )
    selected = []
    for text in by_relevance:
        item = next((m for m in memories if memory_item_text(m) == text), None)
        if item:
            item["last_used"] = now()
            item["uses"] = int(item.get("uses", 0)) + 1
            selected.append(f"- {text} (confidence {float(item.get('confidence', 0.7)):.2f})")
    return selected


def call_ollama(config: AgentConfig, prompt: str) -> str:
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": config.ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": config.ollama_num_predict,
                    "temperature": config.ollama_temperature,
                    "num_ctx": config.ollama_num_ctx,
                },
            },
            timeout=config.ollama_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()["response"].strip()
    except requests.exceptions.ConnectionError:
        return "I'm here, but my local Ollama brain is not running."
    except requests.exceptions.Timeout:
        return "Ollama took too long to respond."
    except requests.exceptions.HTTPError as e:
        return f"Ollama returned an error: {e}"


def build_prompt(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
) -> str:
    personality = config.personality_path.read_text(encoding="utf-8").strip()
    cleaned_latest = clean_slack_text(latest_message)

    relevant_thread = select_relevant_items(
        cleaned_latest,
        thread_context_lines,
        config.max_thread_lines,
        always_keep_last=config.always_keep_recent_thread,
    )
    relevant_channel = select_relevant_items(
        cleaned_latest,
        channel_context_lines,
        config.max_channel_lines,
        always_keep_last=config.always_keep_recent_channel,
    )
    relevant_memory = selected_memory_lines(config, cleaned_latest, memory)

    memory_text = "\n".join(relevant_memory) if relevant_memory else "No relevant memories."
    thread_text = "\n".join(relevant_thread) if relevant_thread else "No relevant thread context."
    channel_text = "\n".join(relevant_channel) if relevant_channel else "No relevant channel context."

    return f"""
{personality}

Relevant memory claims:
{memory_text}

Relevant channel context:
{channel_text}

Relevant thread context:
{thread_text}

Latest message:
{speaker_name}: {cleaned_latest}

Instructions:
- Reply to the latest message only.
- Keep the response brief and Slack-like unless directly asked for detail.
- Treat memory as fallible claims, not guaranteed truth.
- Trust the current Slack context over memory if they conflict.
- Do not invent events, relationships, or claims not supported by the context or memory.
- Do not continue a bot-to-bot riff unless a human clearly asked you to.
- Do not summarize all context unless asked.
- Avoid using people's full names.
""".strip()


def generate_reply(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
) -> str:
    prompt = build_prompt(
        config=config,
        latest_message=latest_message,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
    )
    reply = call_ollama(config, prompt)
    save_memory(config, memory)
    return reply


def should_respond_to_channel_message(config: AgentConfig, text: str) -> bool:
    return text_mentions_agent(config, text) or text_asks_agent_question(config, text)


def should_respond_to_thread_reply(
    config: AgentConfig,
    text: str,
    thread_state: dict[str, Any] | None,
    is_bot_message: bool,
) -> tuple[bool, bool]:
    explicit = text_mentions_agent(config, text) or text_asks_agent_question(config, text)
    if explicit:
        return True, True
    if not thread_state:
        return False, False
    if is_bot_message:
        return False, False
    if int(thread_state.get("auto_reply_count", 0)) >= config.max_auto_replies_per_thread:
        return False, False
    if now() - int(thread_state.get("last_bot_reply_at", 0)) < config.response_cooldown_seconds:
        return False, False
    if text_is_about_agent(config, text, active_thread=True):
        return True, False
    if "?" in text and text_asks_active_followup(text):
        return True, False
    return False, False


def respond_as_agent(
    *,
    config: AgentConfig,
    event: dict[str, Any],
    say,
    client,
    memory: dict[str, Any],
    channel: str,
    thread_ts: str,
    include_channel_context: bool,
    explicit: bool,
) -> None:
    start = time.time()
    text = event.get("text", "")
    speaker_name = get_user_name(config, client, event.get("user"))

    command_reply = handle_memory_commands(config, text, memory, speaker_name)
    if command_reply:
        say(text=command_reply, thread_ts=thread_ts)
        record_bot_reply(config, memory, channel, thread_ts, explicit=True)
        debug(config, f"Memory command replied in {time.time() - start:.2f}s")
        return

    thread_context_lines = fetch_thread_context(config, client, channel, thread_ts)
    channel_context_lines = (
        fetch_channel_context(config, client, channel, event["ts"])
        if include_channel_context
        else []
    )

    reply = generate_reply(
        config=config,
        latest_message=text,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
    )
    say(text=reply, thread_ts=thread_ts)
    record_bot_reply(config, memory, channel, thread_ts, explicit=explicit)
    debug(config, f"Slack reply sent in {time.time() - start:.2f}s")


def create_app(config: AgentConfig) -> App:
    app = App(token=config.slack_bot_token)

    @app.event("app_mention")
    def handle_explicit_mention(event, say, client):
        debug_event(config, "APP_MENTION", event)
        if should_ignore_event(config, event):
            return

        memory = load_memory(config)
        channel = event["channel"]
        thread_ts = get_thread_ts(event)
        mark_thread_active(config, memory, channel, thread_ts)
        respond_as_agent(
            config=config,
            event=event,
            say=say,
            client=client,
            memory=memory,
            channel=channel,
            thread_ts=thread_ts,
            include_channel_context=True,
            explicit=True,
        )

    @app.event("message")
    def handle_message(event, say, client):
        debug_event(config, "MESSAGE", event)
        if should_ignore_event(config, event):
            return

        text = event.get("text", "")
        channel = event["channel"]
        memory = load_memory(config)
        is_thread_reply = "thread_ts" in event
        is_bot_message = bool(event.get("bot_id") or event.get("app_id"))

        if not is_thread_reply:
            if not should_respond_to_channel_message(config, text):
                debug(config, "Ignoring channel message")
                return
            thread_ts = event["ts"]
            mark_thread_active(config, memory, channel, thread_ts)
            respond_as_agent(
                config=config,
                event=event,
                say=say,
                client=client,
                memory=memory,
                channel=channel,
                thread_ts=thread_ts,
                include_channel_context=True,
                explicit=True,
            )
            return

        thread_ts = event["thread_ts"]
        thread_state = get_thread_state(config, memory, channel, thread_ts)
        should_respond, explicit = should_respond_to_thread_reply(config, text, thread_state, is_bot_message)
        if not should_respond:
            debug(config, "Ignoring thread reply")
            return

        mark_thread_active(config, memory, channel, thread_ts)
        respond_as_agent(
            config=config,
            event=event,
            say=say,
            client=client,
            memory=memory,
            channel=channel,
            thread_ts=thread_ts,
            include_channel_context=False,
            explicit=explicit,
        )

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Slack fun agent.")
    parser.add_argument(
        "-agent",
        "--agent",
        default=os.getenv("AGENT", "example"),
        help="Agent name. Loads configuration from agents/<name>/config.json.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_agent_config(args.agent)
    app = create_app(config)
    print(f"{config.display_name} is running as agent '{config.name}'")
    print(f"Agent root: {config.root.resolve()}")
    print(f"Memory: {config.memory_path}")
    print(f"Personality: {config.personality_path}")
    print(f"Ollama model: {config.ollama_model}")
    print(f"Debug logging: {config.debug}")
    SocketModeHandler(app, config.slack_app_token).start()


if __name__ == "__main__":
    main()
