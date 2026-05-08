import argparse
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

DEFAULT_ENABLED_TOOLS = (
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
    "react_to_message",
)

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
    tool_router_model: str
    bot_user_id: str | None
    triggers: tuple[str, ...]
    about_phrases: tuple[str, ...]
    pronoun_about_phrases: tuple[str, ...]
    explicit_only_channels: tuple[str, ...]
    enabled_tools: tuple[str, ...]
    debug: bool
    event_log_enabled: bool
    event_log_path: Path
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
    max_auto_replies_per_thread: int
    ambient_response_chance: float
    thread_ambient_response_chance: float
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
CHANNEL_CACHE: dict[str, str] = {}
CHANNEL_INFO_UNAVAILABLE = False
SEEN_EVENTS = SeenEventCache()


@dataclass(frozen=True)
class ResponseDecision:
    should_respond: bool
    explicit: bool
    reason: str


@dataclass(frozen=True)
class AddressCues:
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ToolContext:
    config: AgentConfig
    client: Any
    memory: dict[str, Any]
    channel: str
    thread_ts: str
    latest_ts: str
    latest_message: str
    speaker_name: str
    thread_context_lines: list[str]
    channel_context_lines: list[str]


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


def default_event_log_path(agent_name: str) -> Path:
    shared = os.getenv("EVENT_LOG_PATH")
    agent_specific = os.getenv(f"{agent_env_prefix(agent_name)}_EVENT_LOG_PATH")
    if agent_specific:
        return Path(agent_specific)
    if shared:
        path = Path(shared)
        return path.with_name(f"{agent_name}.{path.name}")
    return Path(f"logs/{agent_name}.events.jsonl")


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

    ollama_model = raw.get("ollama_model") or get_agent_env(agent_name, "OLLAMA_MODEL", "llama3.1:8b")
    tool_router_model = raw.get("tool_router_model") or get_agent_env(agent_name, "TOOL_ROUTER_MODEL", ollama_model)

    return AgentConfig(
        name=agent_name,
        display_name=display_name,
        root=agent_root,
        memory_path=memory_path,
        personality_path=personality_path,
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        ollama_model=ollama_model,
        tool_router_model=tool_router_model,
        bot_user_id=bot_user_id,
        triggers=tuple({normalize_phrase(t) for t in triggers if str(t).strip()}),
        about_phrases=tuple({normalize_phrase(t) for t in about_phrases if str(t).strip()}),
        pronoun_about_phrases=tuple(raw.get("pronoun_about_phrases", ["he", "him", "his", "they", "them"])),
        explicit_only_channels=tuple({normalize_channel_ref(c) for c in raw.get("explicit_only_channels", []) if str(c).strip()}),
        enabled_tools=tuple(raw.get("enabled_tools", DEFAULT_ENABLED_TOOLS)),
        debug=env_bool("DEBUG", True),
        event_log_enabled=env_bool("EVENT_LOG_ENABLED", False),
        event_log_path=default_event_log_path(agent_name),
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
        max_auto_replies_per_thread=env_int("MAX_AUTO_REPLIES_PER_THREAD", 6),
        ambient_response_chance=env_float("AMBIENT_RESPONSE_CHANCE", 0.15),
        thread_ambient_response_chance=env_float("THREAD_AMBIENT_RESPONSE_CHANCE", 0.35),
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


def event_log(config: AgentConfig, event_type: str, **fields: Any) -> None:
    if not config.event_log_enabled:
        return
    record = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "agent": config.name,
        "event_type": event_type,
        **fields,
    }
    try:
        config.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with config.event_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        debug(config, f"Could not write event log: {e}")


def event_log_message(config: AgentConfig, label: str, event: dict[str, Any]) -> None:
    event_log(
        config,
        label,
        slack_type=event.get("type"),
        subtype=event.get("subtype"),
        user=event.get("user"),
        bot_id=event.get("bot_id"),
        app_id=event.get("app_id"),
        channel=event.get("channel"),
        ts=event.get("ts"),
        event_ts=event.get("event_ts"),
        thread_ts=event.get("thread_ts"),
        text=event.get("text", ""),
    )


def normalize_phrase(text: str) -> str:
    return clean_slack_text(str(text)).lower().replace("’", "'").strip()


def normalize_channel_ref(channel: str) -> str:
    cleaned = str(channel).strip().lower()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    return cleaned


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


def agent_trigger_pattern(config: AgentConfig) -> str:
    triggers = [
        normalize_phrase(t)
        for t in config.triggers
        if t and not t.startswith("@") and not re.fullmatch(r"u[a-z0-9]+", normalize_phrase(t))
    ]
    triggers = sorted(set(triggers), key=len, reverse=True)
    return "|".join(re.escape(t) for t in triggers)


def agent_address_cues(config: AgentConfig, text: str) -> AddressCues:
    cleaned = normalize_phrase(text)
    reasons = []
    score = 0

    if any(t.startswith("@") and phrase_in_text(t, text) for t in config.triggers):
        return AddressCues(5, ("mention",))

    trigger_pattern = agent_trigger_pattern(config)
    if not trigger_pattern:
        return AddressCues(0, ())

    address_words = (
        "what|why|how|when|where|who|can|could|would|should|do|does|did|"
        "are|is|tell|say|reply|respond|remember|forget|show|give|help|please"
    )
    greeting = r"hey|hi|hello|yo|sup|ok|okay|alright|gm|gn"
    question_words = r"what|why|how|when|where|who|can|could|would|should|do|does|did|are|is"

    if re.fullmatch(rf"({greeting})?\s*({trigger_pattern})\s*[!.?]*", cleaned):
        score += 4
        reasons.append("name_ping")
    elif re.search(rf"^({greeting})\s+({trigger_pattern})\b", cleaned):
        score += 4
        reasons.append("greeting")
    elif re.search(rf"^({trigger_pattern})\b\s*[,:\-]", cleaned):
        score += 3
        reasons.append("address_prefix")
    elif re.search(rf"^({trigger_pattern})\b", cleaned):
        score += 2
        reasons.append("starts_with_name")

    if re.search(rf"\b({trigger_pattern})\b\s+({address_words})\b", cleaned):
        score += 2
        reasons.append("name_then_request")
    if re.search(rf"\b({question_words})\b.*\b({trigger_pattern})\b", cleaned):
        score += 2
        reasons.append("question_about_agent")
    if "?" in cleaned and re.search(rf"\b({trigger_pattern})\b", cleaned):
        score += 1
        reasons.append("question_mark_with_name")

    passive_verbs = r"asked|told|mentioned|saw|heard|met|called|dm'd|messaged|pinged"
    passive_preps = r"about|from|with|for|to"
    if re.search(rf"\b({passive_verbs}|{passive_preps})\b\s+({trigger_pattern})\b", cleaned):
        score -= 3
        reasons.append("passive_reference")
    if re.search(rf"\b({trigger_pattern})\b\s+(said|told|asked|mentioned|was|is|has|had)\b", cleaned):
        score -= 2
        reasons.append("third_person_reference")

    return AddressCues(score, tuple(reasons))


def text_directly_addresses_agent(config: AgentConfig, text: str) -> bool:
    return agent_address_cues(config, text).score >= 2


def text_asks_agent_question(config: AgentConfig, text: str) -> bool:
    cues = agent_address_cues(config, text)
    return (
        "question_about_agent" in cues.reasons
        or ("question_mark_with_name" in cues.reasons and cues.score >= 1)
    )


def text_is_passive_agent_mention(config: AgentConfig, text: str) -> bool:
    if text_is_social_ack(text):
        return False
    return text_mentions_agent(config, text) and not (
        text_directly_addresses_agent(config, text) or text_asks_agent_question(config, text)
    )


def text_is_about_agent(config: AgentConfig, text: str, active_thread: bool) -> bool:
    cleaned = normalize_phrase(text)
    if any(phrase in cleaned for phrase in config.about_phrases):
        return True
    return active_thread and any(phrase_in_text(p, cleaned) for p in config.pronoun_about_phrases)


def text_asks_active_followup(text: str) -> bool:
    cleaned = normalize_phrase(text)
    if re.search(r"\b(what|why|how|can|could|would|should|do|does|did|are|is)\b.*\byou\b", cleaned):
        return True
    return any(p in cleaned for p in ["what do you think", "thoughts?", "wdyt", "right?", "yes?", "no?"])


def text_invites_room_response(text: str) -> bool:
    cleaned = normalize_phrase(text)
    if len(cleaned) < 12:
        return False
    if any(p in cleaned for p in ["what do yall think", "what do you all think", "thoughts?", "wdyt", "any ideas", "anyone know", "does anyone"]):
        return True
    if "?" not in cleaned:
        return False
    return bool(
        re.search(r"\b(anyone|anybody|someone|somebody|yall|you all|we|us)\b", cleaned)
        or re.search(r"\b(should|can|could|would)\s+(we|i|someone|anyone)\b", cleaned)
    )


def text_is_social_ack(text: str) -> bool:
    cleaned = normalize_phrase(text)
    return any(
        phrase in cleaned
        for phrase in [
            "thank you",
            "thanks",
            "ty",
            "congrats",
            "congratulations",
            "nice",
            "yay",
            "hell yeah",
            "lets go",
            "lfg",
            "we shipped",
            "that worked",
        ]
    )


def text_is_reaction_worthy(text: str) -> bool:
    cleaned = normalize_phrase(text)
    return text_is_social_ack(text) or any(
        phrase in cleaned
        for phrase in [
            "shipped",
            "huge win",
            "big win",
            "it worked",
            "worked",
            "lets go",
            "lfg",
            "great job",
            "good job",
            "nailed it",
            "amazing",
            "awesome",
            "love this",
            "lol",
            "lmao",
            "rip",
        ]
    )


def deterministic_chance(key: str, chance: float) -> bool:
    chance = max(0.0, min(1.0, chance))
    if chance <= 0:
        return False
    if chance >= 1:
        return True
    digest = hashlib.sha1(normalize_phrase(key).encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) / 0xFFFFFFFF < chance


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


def get_event_speaker_name(config: AgentConfig, client, event: dict[str, Any]) -> str:
    user_id = event.get("user")
    if user_id:
        return get_user_name(config, client, user_id)
    bot_profile = event.get("bot_profile") or {}
    return (
        event.get("username")
        or bot_profile.get("name")
        or bot_profile.get("real_name")
        or event.get("bot_id")
        or "unknown"
    )


def get_channel_name(config: AgentConfig, client, channel_id: str) -> str | None:
    global CHANNEL_INFO_UNAVAILABLE
    if CHANNEL_INFO_UNAVAILABLE:
        return None
    if channel_id in CHANNEL_CACHE:
        return CHANNEL_CACHE[channel_id]
    try:
        result = client.conversations_info(channel=channel_id)
        channel = result.get("channel", {})
        name = channel.get("name") or channel.get("name_normalized")
        if name:
            CHANNEL_CACHE[channel_id] = normalize_channel_ref(name)
            return CHANNEL_CACHE[channel_id]
    except Exception as e:
        if "missing_scope" in str(e):
            CHANNEL_INFO_UNAVAILABLE = True
        debug(config, f"Could not fetch channel info for {channel_id}: {e}")
    return None


def channel_is_explicit_only(config: AgentConfig, client, channel_id: str) -> bool:
    if not config.explicit_only_channels:
        return False
    configured = set(config.explicit_only_channels)
    normalized_id = normalize_channel_ref(channel_id)
    if normalized_id in configured:
        return True
    if channel_id.upper().startswith(("C", "G")) and all(c.upper().startswith(("C", "G")) for c in configured):
        return False
    channel_name = get_channel_name(config, client, channel_id)
    return bool(channel_name and channel_name in configured)


def format_slack_timestamp(ts: str | None) -> str:
    if not ts:
        return "unknown time"
    try:
        timestamp = float(ts)
    except ValueError:
        return "unknown time"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")


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
        lines.append(f"[{format_slack_timestamp(msg.get('ts'))}] {speaker}: {text}")
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


def tool_get_time(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    timezone = str(args.get("timezone") or os.getenv("TZ") or "America/New_York")
    try:
        current = datetime.now(ZoneInfo(timezone))
    except ZoneInfoNotFoundError:
        current = datetime.now().astimezone()
        timezone = current.tzname() or "local"
    return {
        "timezone": timezone,
        "iso": current.isoformat(timespec="seconds"),
        "readable": current.strftime("%A, %B %d, %Y at %I:%M %p %Z"),
    }


def tool_search_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or ctx.latest_message)
    limit = max(1, min(int(args.get("limit") or ctx.config.max_memory_lines), 10))
    memories = [
        item for item in ctx.memory.get("memories", [])
        if float(item.get("confidence", 0.7)) >= 0.4
    ]
    scored = [
        (relevance_score(query, memory_item_text(item)), item)
        for item in memories
    ]
    matches = [
        item for score, item in sorted(scored, key=lambda pair: pair[0], reverse=True)
        if score > 0
    ][:limit]
    return {
        "query": query,
        "matches": [
            {
                "text": memory_item_text(item),
                "confidence": float(item.get("confidence", 0.7)),
                "source": item.get("source", "memory"),
            }
            for item in matches
        ],
    }


def tool_update_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    fact = str(args.get("text") or args.get("fact") or "").strip()
    if not fact:
        return {"saved": False, "error": "Missing memory text."}

    confidence = args.get("confidence")
    if confidence is None:
        confidence = memory_confidence(fact)
    confidence = max(0.0, min(float(confidence), 1.0))

    item = make_memory_item(fact, ctx.speaker_name, confidence)
    memories = ctx.memory.setdefault("memories", [])
    existing = next((m for m in memories if m.get("id") == item["id"]), None)
    if existing:
        existing.update({
            "text": fact,
            "updated_at": now(),
            "confidence": max(float(existing.get("confidence", 0.7)), confidence),
            "source": ctx.speaker_name,
        })
        action = "updated"
    else:
        memories.append(item)
        action = "created"

    prune_memory(ctx.config, ctx.memory)
    save_memory(ctx.config, ctx.memory)
    return {"saved": True, "action": action, "text": fact, "confidence": confidence}


def tool_summarize_thread(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    lines = ctx.thread_context_lines[-ctx.config.thread_fetch_limit:]
    if not lines:
        return {"summary": "No thread context available."}
    focus = str(args.get("focus") or ctx.latest_message)
    prompt = f"""
Summarize this Slack thread in 3 short bullets. Focus on: {focus}

Thread:
{chr(10).join(lines)}
""".strip()
    return {"summary": call_ollama(ctx.config, prompt)}


def tool_get_channel_context(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or ctx.config.max_channel_lines), 20))
    lines = ctx.channel_context_lines
    if not lines:
        lines = fetch_channel_context(ctx.config, ctx.client, ctx.channel, ctx.latest_ts)
    lines = lines[-limit:]
    return {
        "lines": lines,
        "note": "Most recent channel messages before the latest message.",
    }


def tool_get_user_profile(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_user = str(args.get("user") or args.get("name") or "").strip()
    target = clean_slack_text(raw_user)
    if not target:
        return {"found": False, "error": "Missing user."}

    user_id_match = re.search(r"@?(U[A-Z0-9]+)", target, flags=re.I)
    if user_id_match:
        user_id = user_id_match.group(1).upper()
        try:
            result = ctx.client.users_info(user=user_id)
            user = result.get("user", {})
        except Exception as e:
            return {"found": False, "error": str(e)}
        profile = user.get("profile", {})
        return {
            "found": True,
            "id": user.get("id"),
            "name": profile.get("display_name") or profile.get("real_name") or user.get("name"),
            "real_name": profile.get("real_name"),
            "title": profile.get("title"),
            "tz": user.get("tz"),
            "is_bot": bool(user.get("is_bot")),
        }

    query = normalize_phrase(target).lstrip("@")
    try:
        result = ctx.client.users_list(limit=200)
    except Exception as e:
        return {"found": False, "error": str(e)}

    matches = []
    for user in result.get("members", []):
        profile = user.get("profile", {})
        names = [
            user.get("name", ""),
            profile.get("display_name", ""),
            profile.get("real_name", ""),
        ]
        if any(query and query in normalize_phrase(name) for name in names):
            matches.append({
                "id": user.get("id"),
                "name": profile.get("display_name") or profile.get("real_name") or user.get("name"),
                "real_name": profile.get("real_name"),
                "title": profile.get("title"),
                "tz": user.get("tz"),
                "is_bot": bool(user.get("is_bot")),
            })
    return {"found": bool(matches), "matches": matches[:5]}


def tool_list_recent_threads(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 8), 20))
    history_limit = max(limit * 4, 30)
    try:
        result = ctx.client.conversations_history(
            channel=ctx.channel,
            latest=ctx.latest_ts,
            limit=history_limit,
            inclusive=False,
        )
    except Exception as e:
        return {"threads": [], "error": str(e)}

    threads = OrderedDict()
    for msg in result.get("messages", []):
        root_ts = msg.get("thread_ts") or msg.get("ts")
        if not root_ts or root_ts in threads:
            continue
        reply_count = int(msg.get("reply_count", 0) or 0)
        if reply_count <= 0 and msg.get("thread_ts") is None:
            continue
        speaker = get_event_speaker_name(ctx.config, ctx.client, msg)
        threads[root_ts] = {
            "thread_ts": root_ts,
            "time": format_slack_timestamp(root_ts),
            "speaker": speaker,
            "reply_count": reply_count,
            "text": clean_slack_text(msg.get("text", ""))[:240],
        }
        if len(threads) >= limit:
            break
    return {"threads": list(threads.values())}


def tool_save_thread_summary(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        if not ctx.thread_context_lines:
            return {"saved": False, "error": "No thread context available."}
        prompt = f"""
Summarize this Slack thread as one durable memory sentence. Include the date if useful.

Thread:
{chr(10).join(ctx.thread_context_lines)}
""".strip()
        summary = call_ollama(ctx.config, prompt)

    text = f"Thread summary from {format_slack_timestamp(ctx.thread_ts)}: {summary}"
    item = make_memory_item(text, "thread_summary", float(args.get("confidence") or 0.8))
    memories = ctx.memory.setdefault("memories", [])
    existing = next((m for m in memories if m.get("id") == item["id"]), None)
    if existing:
        existing.update({"text": text, "updated_at": now(), "confidence": max(float(existing.get("confidence", 0.7)), item["confidence"])})
        action = "updated"
    else:
        memories.append(item)
        action = "created"
    prune_memory(ctx.config, ctx.memory)
    save_memory(ctx.config, ctx.memory)
    return {"saved": True, "action": action, "text": text}


def tool_search_channel_history(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"matches": [], "error": "Missing query."}
    limit = max(1, min(int(args.get("limit") or 8), 20))
    history_limit = max(limit * 8, 50)
    try:
        result = ctx.client.conversations_history(
            channel=ctx.channel,
            latest=ctx.latest_ts,
            limit=history_limit,
            inclusive=False,
        )
    except Exception as e:
        return {"matches": [], "error": str(e)}

    messages = slack_messages_to_lines(ctx.config, ctx.client, list(reversed(result.get("messages", []))))
    scored = [
        (relevance_score(query, line), line)
        for line in messages
    ]
    matches = [
        line for score, line in sorted(scored, key=lambda item: item[0], reverse=True)
        if score > 0
    ][:limit]
    return {"query": query, "matches": matches}


def tool_set_reminder_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    text = str(args.get("text") or args.get("note") or "").strip()
    due = str(args.get("due") or args.get("when") or "").strip()
    if not text:
        return {"saved": False, "error": "Missing reminder text."}
    reminder_text = f"Reminder note"
    if due:
        reminder_text += f" for {due}"
    reminder_text += f": {text}"
    item = make_memory_item(reminder_text, "reminder_note", float(args.get("confidence") or 0.75))
    item["kind"] = "reminder_note"
    if due:
        item["due"] = due
    ctx.memory.setdefault("memories", []).append(item)
    prune_memory(ctx.config, ctx.memory)
    save_memory(ctx.config, ctx.memory)
    return {"saved": True, "text": reminder_text}


def tool_react_to_message(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    emoji = str(args.get("emoji") or "").strip().strip(":")
    if not emoji:
        return {"reacted": False, "error": "Missing emoji."}
    target_ts = str(args.get("ts") or ctx.latest_ts)
    try:
        result = ctx.client.reactions_get(channel=ctx.channel, timestamp=target_ts, full=False)
        message = result.get("message", {})
        for reaction in message.get("reactions", []):
            if reaction.get("name") == emoji:
                return {"reacted": False, "emoji": emoji, "reason": "already_present", "ts": target_ts, "reply_text": ""}
    except Exception as e:
        debug(ctx.config, f"Could not inspect existing reactions: {e}")
    try:
        ctx.client.reactions_add(channel=ctx.channel, timestamp=target_ts, name=emoji)
    except Exception as e:
        return {"reacted": False, "emoji": emoji, "error": str(e), "reply_text": f"I tried to react with :{emoji}:, but Slack rejected it."}
    return {"reacted": True, "emoji": emoji, "ts": target_ts, "reply_text": ""}


TOOL_HANDLERS = {
    "get_time": tool_get_time,
    "search_memory": tool_search_memory,
    "update_memory": tool_update_memory,
    "summarize_thread": tool_summarize_thread,
    "get_channel_context": tool_get_channel_context,
    "get_user_profile": tool_get_user_profile,
    "list_recent_threads": tool_list_recent_threads,
    "save_thread_summary": tool_save_thread_summary,
    "search_channel_history": tool_search_channel_history,
    "set_reminder_note": tool_set_reminder_note,
    "react_to_message": tool_react_to_message,
}


TOOL_DESCRIPTIONS = {
    "get_time": "Get the current date/time for time, date, elapsed-time, or relative-time questions. Arguments: timezone, optional IANA timezone.",
    "search_memory": "Search this agent's memory. Arguments: query, optional limit.",
    "update_memory": "Save one durable memory claim. Arguments: text or fact, optional confidence 0-1.",
    "summarize_thread": "Summarize the current Slack thread. Arguments: optional focus.",
    "get_channel_context": "Inspect recent channel messages before this event. Arguments: optional limit.",
    "get_user_profile": "Look up a Slack user's basic profile. Arguments: user/name.",
    "list_recent_threads": "List recently active threads in the current channel. Arguments: optional limit.",
    "save_thread_summary": "Save a durable memory summary of the current thread. Arguments: optional summary/confidence.",
    "search_channel_history": "Search recent messages in the current channel. Arguments: query, optional limit.",
    "set_reminder_note": "Save a local reminder note with optional due text. Arguments: text/note, optional due/when.",
    "react_to_message": "Add a natural emoji reaction to the current or specified message. Arguments: emoji, optional ts.",
}


def selected_memory_lines(config: AgentConfig, latest_message: str, memory: dict[str, Any]) -> list[str]:
    memories = [
        item for item in memory.get("memories", [])
        if float(item.get("confidence", 0.7)) >= 0.4
    ]
    by_relevance = select_relevant_items(
        latest_message,
        [memory_item_text(item) for item in memories],
        config.max_memory_lines,
        always_keep_last=0,
    )
    selected = []
    for text in by_relevance:
        item = next((m for m in memories if memory_item_text(m) == text), None)
        if item:
            item["last_used"] = now()
            item["uses"] = int(item.get("uses", 0)) + 1
            selected.append(f"- {text} (confidence {float(item.get('confidence', 0.7)):.2f})")
    return selected


def call_ollama(
    config: AgentConfig,
    prompt: str,
    model: str | None = None,
    num_predict: int | None = None,
    temperature: float | None = None,
) -> str:
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model or config.ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": num_predict or config.ollama_num_predict,
                    "temperature": config.ollama_temperature if temperature is None else temperature,
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


def extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        objects = []
        for match in re.finditer(r"\{", cleaned):
            try:
                candidate, _ = decoder.raw_decode(cleaned[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                objects.append(candidate)
        if not objects:
            return None
        tool_calls = [obj for obj in objects if obj.get("type") == "tool_call"]
        return tool_calls[-1] if tool_calls else objects[-1]
    return data if isinstance(data, dict) else None


def enabled_tool_descriptions(config: AgentConfig) -> str:
    lines = []
    for name in config.enabled_tools:
        description = TOOL_DESCRIPTIONS.get(name)
        if description:
            lines.append(f"- {name}: {description}")
    return "\n".join(lines) if lines else "No tools enabled."


def tool_instruction_text(config: AgentConfig) -> str:
    if not config.enabled_tools:
        return ""
    return f"""

Available tools:
{enabled_tool_descriptions(config)}

Use tools based on intent, not exact wording:
- Use get_time for current time/date, deadlines, elapsed time, "how long ago", "when was that", or relative time questions.
- Use search_memory when the user asks what you know/remember about a specific person, topic, preference, or past fact.
- Use update_memory when the user asks you to remember something or clearly states a durable fact you should keep.
- Use summarize_thread when the user asks for a recap, summary, decision, or what happened in this thread.
- Use get_channel_context when the user asks what they missed, what happened recently, or needs recent channel context.

If a tool would clearly help, respond with only JSON:
{{"type":"tool_call","tool":"tool_name","arguments":{{}}}}

If no tool is needed, respond with only JSON:
{{"type":"reply","text":"your Slack reply"}}
""".rstrip()


def build_prompt(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
    tool_mode: bool = True,
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
- Act like a real participant in the chat, not a support assistant.
- If the latest message only mentions you in passing, keep the reply especially short or acknowledge lightly.
- Treat memory as fallible claims, not guaranteed truth.
- Trust the current Slack context over memory if they conflict.
- Do not mention memory or remembered facts unless directly relevant or asked.
- Do not invent events, relationships, or claims not supported by the context or memory.
- Do not continue a bot-to-bot riff unless a human clearly asked you to.
- Do not summarize all context unless asked.
- Avoid using people's full names.
{tool_instruction_text(config) if tool_mode else ""}
""".strip()


def build_tool_result_prompt(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    tool_name: str,
    tool_result: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
    explicit: bool = True,
) -> str:
    base = build_prompt(
        config=config,
        latest_message=latest_message,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
        tool_mode=False,
    )
    return f"""
{base}

Tool used: {tool_name}
Tool result:
{json.dumps(tool_result, ensure_ascii=False, indent=2)}

Write the final Slack reply using the tool result as the source of truth.
- Do not contradict the tool result.
- If the tool result has matches, threads, lines, or profile fields, use those concrete details.
- If the tool result is empty, say that directly and briefly.
- Do not pretend you checked something beyond the tool result.
- Do not mention JSON or tools unless the user asked.
- {"Keep it especially short because this was not an explicit request." if not explicit else "Answer the user's request directly."}
""".strip()


def direct_tool_reply(tool_name: str, tool_result: dict[str, Any], explicit: bool) -> str | None:
    if tool_name == "list_recent_threads":
        threads = tool_result.get("threads") or []
        if not threads:
            return "I don't see any active threads recently."
        lines = [
            f"- {thread.get('time')}: {thread.get('speaker')} - {thread.get('text')} ({thread.get('reply_count')} replies)"
            for thread in threads[:8]
        ]
        return "Recent active threads:\n" + "\n".join(lines)

    if tool_name == "search_channel_history":
        matches = tool_result.get("matches") or []
        query = tool_result.get("query") or "that"
        if not matches:
            return f"I didn't find recent channel mentions of {query}."
        lines = [f"- {match}" for match in matches[:5]]
        return "I found these recent mentions:\n" + "\n".join(lines)

    if tool_name == "get_channel_context":
        lines = tool_result.get("lines") or []
        if not lines:
            return "I don't see much recent channel context."
        return "Recent channel context:\n" + "\n".join(f"- {line}" for line in lines[-6:])

    return None


def run_tool_call(tool_call: dict[str, Any], ctx: ToolContext) -> tuple[str, dict[str, Any]] | None:
    tool_name = str(tool_call.get("tool") or "")
    if tool_name not in ctx.config.enabled_tools:
        event_log(ctx.config, "tool_rejected", tool=tool_name, reason="not_enabled", raw=tool_call)
        return None
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        event_log(ctx.config, "tool_rejected", tool=tool_name, reason="missing_handler", raw=tool_call)
        return None
    arguments = tool_call.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    debug(ctx.config, f"Running tool {tool_name} with args={arguments!r}")
    result = handler(arguments, ctx)
    event_log(ctx.config, "tool_result", tool=tool_name, arguments=arguments, result=result)
    return tool_name, result


def make_tool_call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "tool_call", "tool": tool, "arguments": arguments or {}}


def likely_reaction_emoji(text: str) -> str:
    cleaned = normalize_phrase(text)
    if any(p in cleaned for p in ["thank you", "thanks", "ty", "pray"]):
        return "pray"
    if any(p in cleaned for p in ["congrats", "congratulations", "shipped", "huge win", "big win", "lets go", "lfg"]):
        return "tada"
    if any(p in cleaned for p in ["worked", "great job", "good job", "nailed it"]):
        return "raised_hands"
    if any(p in cleaned for p in ["lol", "lmao"]):
        return "joy"
    if "rip" in cleaned:
        return "pray"
    return "thumbsup"


def deterministic_tool_route(ctx: ToolContext) -> dict[str, Any] | None:
    cleaned = normalize_phrase(ctx.latest_message)
    text = clean_slack_text(ctx.latest_message)

    if re.search(r"\b(when|how long ago)\b.*\b(last\s+)?mention", cleaned):
        user_match = re.search(r"@?U[A-Z0-9]+", text, flags=re.I)
        if user_match:
            return make_tool_call("search_channel_history", {"query": user_match.group(0), "limit": 8})
        query = re.sub(r"\b(wee|irdnennam|when|how long ago|did|i|last|mention|mentioned)\b", " ", cleaned)
        query = re.sub(r"\s+", " ", query).strip(" ?.,")
        return make_tool_call("search_channel_history", {"query": query or cleaned, "limit": 8})

    if re.search(r"\b(who is|who's|timezone|time zone|profile|title)\b", cleaned) and re.search(r"@?U[A-Z0-9]+", text, flags=re.I):
        user = re.search(r"@?U[A-Z0-9]+", text, flags=re.I).group(0)
        return make_tool_call("get_user_profile", {"user": user})

    if any(p in cleaned for p in ["what did i miss", "what'd i miss", "catch me up", "what happened recently"]):
        return make_tool_call("get_channel_context", {"limit": 10})

    if re.search(r"\b(did we|have we|anyone)\b.*\b(talk|mention|discuss)\b", cleaned):
        query = re.sub(r"\b(wee|irdnennam|did we|have we|anyone|talk about|talked about|mention|mentioned|discuss|discussed|recently)\b", " ", cleaned)
        query = re.sub(r"\s+", " ", query).strip(" ?.,")
        return make_tool_call("search_channel_history", {"query": query or cleaned, "limit": 8})

    if any(p in cleaned for p in ["what threads are active", "active threads", "recent threads", "what are people talking about"]):
        return make_tool_call("list_recent_threads", {"limit": 8})

    if text_is_reaction_worthy(ctx.latest_message):
        return make_tool_call("react_to_message", {"emoji": likely_reaction_emoji(ctx.latest_message)})

    return None


def build_tool_router_prompt(ctx: ToolContext) -> str:
    thread_text = "\n".join(ctx.thread_context_lines[-ctx.config.max_thread_lines:]) or "No thread context."
    channel_text = "\n".join(ctx.channel_context_lines[-ctx.config.max_channel_lines:]) or "No channel context."
    return f"""
You are a routing model for a Slack chat agent. Decide if the latest message needs a tool.

Available tools:
{enabled_tool_descriptions(ctx.config)}

Use tools based on intent, not exact wording:
- get_time: current time/date, deadlines, elapsed time, "how long ago", "when was that", relative time.
- search_memory: questions about remembered facts, preferences, people, or past claims.
- update_memory: requests to remember something or clear durable facts worth storing.
- summarize_thread: recap, summarize, decisions, action items, or what happened in this thread.
- get_channel_context: what did I miss, what happened recently, recent channel context.
- get_user_profile: who is this person, what is their Slack profile/title/timezone, or resolving a user mention.
- list_recent_threads: what threads are active, what are people talking about, recent discussions.
- save_thread_summary: remember/save the decision or outcome from this thread.
- search_channel_history: did we talk about a topic recently, find recent mentions in this channel.
- set_reminder_note: remember to do something later, remind me/us, keep a dated note.
- react_to_message: user asks you to react, or a lightweight emoji reaction is more natural than text. Use for celebrations, wins, thanks, jokes, agreement, sympathy, or acknowledgements. Pick common Slack emoji names like tada, raised_hands, clap, heart, joy, fire, eyes, white_check_mark, thumbsup, or pray. Do not react to every positive message.

Recent channel context:
{channel_text}

Thread context:
{thread_text}

Latest message:
{ctx.speaker_name}: {clean_slack_text(ctx.latest_message)}

Return only JSON. Use one of:
{{"type":"none"}}
{{"type":"tool_call","tool":"get_time","arguments":{{"timezone":"America/New_York"}}}}
{{"type":"tool_call","tool":"search_memory","arguments":{{"query":"topic","limit":5}}}}
{{"type":"tool_call","tool":"update_memory","arguments":{{"text":"fact","confidence":0.7}}}}
{{"type":"tool_call","tool":"summarize_thread","arguments":{{"focus":"what to summarize"}}}}
{{"type":"tool_call","tool":"get_channel_context","arguments":{{"limit":10}}}}
{{"type":"tool_call","tool":"get_user_profile","arguments":{{"user":"name or user id"}}}}
{{"type":"tool_call","tool":"list_recent_threads","arguments":{{"limit":8}}}}
{{"type":"tool_call","tool":"save_thread_summary","arguments":{{"summary":"decision or outcome"}}}}
{{"type":"tool_call","tool":"search_channel_history","arguments":{{"query":"topic","limit":8}}}}
{{"type":"tool_call","tool":"set_reminder_note","arguments":{{"text":"thing to remember","due":"when"}}}}
{{"type":"tool_call","tool":"react_to_message","arguments":{{"emoji":"thumbsup"}}}}

Do not include explanations. Do not include multiple JSON objects.
Prefer a tool call for profile, "what did I miss", channel-history, active-thread, reminder, time, and recap requests even if recent context seems partially useful.
Choose none only for greetings, opinions, casual replies, or when no listed tool would materially improve the answer. Prefer react_to_message over a text reply when a small social acknowledgement is enough.
""".strip()


def route_tool_call(ctx: ToolContext) -> dict[str, Any] | None:
    if not ctx.config.enabled_tools:
        event_log(ctx.config, "tool_router", decision="none", reason="no_enabled_tools")
        return None
    deterministic = deterministic_tool_route(ctx)
    if deterministic and deterministic.get("tool") in ctx.config.enabled_tools:
        event_log(ctx.config, "tool_router", decision="tool_call", route="deterministic", tool=deterministic["tool"], parsed=deterministic)
        return deterministic
    raw = call_ollama(
        ctx.config,
        build_tool_router_prompt(ctx),
        model=ctx.config.tool_router_model,
        num_predict=180,
        temperature=0.0,
    )
    parsed = extract_json_object(raw)
    if not parsed or parsed.get("type") != "tool_call":
        event_log(ctx.config, "tool_router", decision="none", raw=raw[:1000], parsed=parsed)
        return None
    tool_name = str(parsed.get("tool") or "")
    if tool_name not in ctx.config.enabled_tools:
        event_log(ctx.config, "tool_router", decision="rejected", reason="not_enabled", raw=raw[:1000], parsed=parsed)
        return None
    event_log(ctx.config, "tool_router", decision="tool_call", route="model", tool=tool_name, parsed=parsed)
    return parsed


def generate_reply(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
    client: Any,
    channel: str,
    thread_ts: str,
    latest_ts: str,
    explicit: bool,
) -> str:
    tool_context = ToolContext(
        config=config,
        client=client,
        memory=memory,
        channel=channel,
        thread_ts=thread_ts,
        latest_ts=latest_ts,
        latest_message=latest_message,
        speaker_name=speaker_name,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
    )
    routed_tool_call = route_tool_call(tool_context)
    tool_run = run_tool_call(routed_tool_call, tool_context) if routed_tool_call else None

    if tool_run:
        tool_name, tool_result = tool_run
        if tool_name == "react_to_message" and not explicit:
            save_memory(config, memory)
            return str(tool_result.get("reply_text") or "").strip()
        direct_reply = direct_tool_reply(tool_name, tool_result, explicit)
        if direct_reply:
            save_memory(config, memory)
            return direct_reply
        reply = call_ollama(
            config,
            build_tool_result_prompt(
                config=config,
                latest_message=latest_message,
                speaker_name=speaker_name,
                memory=memory,
                tool_name=tool_name,
                tool_result=tool_result,
                thread_context_lines=thread_context_lines,
                channel_context_lines=channel_context_lines,
                explicit=explicit,
            ),
        )
        save_memory(config, memory)
        return reply

    prompt = build_prompt(
        config=config,
        latest_message=latest_message,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
        tool_mode=False,
    )
    reply = call_ollama(config, prompt)
    save_memory(config, memory)
    return reply


def should_respond_to_channel_message(
    config: AgentConfig,
    text: str,
    ambient_allowed: bool = True,
    is_bot_message: bool = False,
) -> ResponseDecision:
    if text_directly_addresses_agent(config, text) or text_asks_agent_question(config, text):
        return ResponseDecision(True, True, "direct")
    if text_is_passive_agent_mention(config, text):
        return ResponseDecision(False, False, "passive_name_mention")
    if is_bot_message:
        return ResponseDecision(False, False, "bot_ambient_suppressed")
    if not ambient_allowed:
        return ResponseDecision(False, False, "explicit_only_channel")
    if text_is_reaction_worthy(text):
        return ResponseDecision(True, False, "reaction_worthy")
    if text_invites_room_response(text) and deterministic_chance(text, config.ambient_response_chance):
        return ResponseDecision(True, False, "ambient_room_prompt")
    return ResponseDecision(False, False, "not_relevant")


def should_respond_to_thread_reply(
    config: AgentConfig,
    text: str,
    thread_state: dict[str, Any] | None,
    is_bot_message: bool,
    ambient_allowed: bool = True,
) -> ResponseDecision:
    explicit = text_directly_addresses_agent(config, text) or text_asks_agent_question(config, text)
    if explicit:
        return ResponseDecision(True, True, "direct")
    if not thread_state:
        return ResponseDecision(False, False, "inactive_thread")
    if is_bot_message:
        return ResponseDecision(False, False, "bot_message")
    if int(thread_state.get("auto_reply_count", 0)) >= config.max_auto_replies_per_thread:
        return ResponseDecision(False, False, "thread_auto_reply_limit")
    if text_mentions_agent(config, text) and text_is_social_ack(text):
        return ResponseDecision(True, False, "social_ack")
    if text_is_passive_agent_mention(config, text):
        return ResponseDecision(False, False, "passive_name_mention")
    if not ambient_allowed:
        return ResponseDecision(False, False, "explicit_only_channel")
    if text_is_reaction_worthy(text):
        return ResponseDecision(True, False, "reaction_worthy")
    if text_asks_active_followup(text):
        return ResponseDecision(True, False, "active_followup")
    if text_invites_room_response(text) and deterministic_chance(text, config.thread_ambient_response_chance):
        return ResponseDecision(True, False, "ambient_thread_prompt")
    if text_is_about_agent(config, text, active_thread=True) and deterministic_chance(text, config.thread_ambient_response_chance / 2):
        return ResponseDecision(True, False, "ambient_about_agent")
    return ResponseDecision(False, False, "not_relevant")


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
    speaker_name = get_event_speaker_name(config, client, event)

    command_reply = handle_memory_commands(config, text, memory, speaker_name)
    if command_reply:
        say(text=command_reply, thread_ts=thread_ts)
        record_bot_reply(config, memory, channel, thread_ts, explicit=True)
        event_log(config, "memory_command_reply", channel=channel, thread_ts=thread_ts, text=text, reply=command_reply)
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
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        latest_ts=event["ts"],
        explicit=explicit,
    )
    if not reply:
        record_bot_reply(config, memory, channel, thread_ts, explicit=explicit)
        event_log(
            config,
            "reply_skipped",
            channel=channel,
            thread_ts=thread_ts,
            latest_ts=event.get("ts"),
            explicit=explicit,
            elapsed_seconds=round(time.time() - start, 2),
            text=text,
            reason="empty_reply_after_tool",
        )
        debug(config, f"Slack reply skipped in {time.time() - start:.2f}s")
        return

    say(text=reply, thread_ts=thread_ts)
    record_bot_reply(config, memory, channel, thread_ts, explicit=explicit)
    event_log(
        config,
        "reply_sent",
        channel=channel,
        thread_ts=thread_ts,
        latest_ts=event.get("ts"),
        explicit=explicit,
        elapsed_seconds=round(time.time() - start, 2),
        text=text,
        reply=reply,
    )
    debug(config, f"Slack reply sent in {time.time() - start:.2f}s")


def create_app(config: AgentConfig) -> App:
    app = App(token=config.slack_bot_token)

    @app.event("app_mention")
    def handle_explicit_mention(event, say, client):
        debug_event(config, "APP_MENTION", event)
        event_log_message(config, "app_mention_received", event)
        if should_ignore_event(config, event):
            event_log(config, "ignored", source="app_mention", reason="should_ignore_event", ts=event.get("ts"))
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
        event_log_message(config, "message_received", event)
        if should_ignore_event(config, event):
            event_log(config, "ignored", source="message", reason="should_ignore_event", ts=event.get("ts"))
            return

        text = event.get("text", "")
        channel = event["channel"]
        memory = load_memory(config)
        is_thread_reply = "thread_ts" in event
        is_bot_message = bool(event.get("bot_id") or event.get("app_id"))
        ambient_allowed = not channel_is_explicit_only(config, client, channel)

        if not is_thread_reply:
            decision = should_respond_to_channel_message(
                config,
                text,
                ambient_allowed=ambient_allowed,
                is_bot_message=is_bot_message,
            )
            event_log(
                config,
                "response_decision",
                surface="channel",
                channel=channel,
                ts=event.get("ts"),
                text=text,
                ambient_allowed=ambient_allowed,
                is_bot_message=is_bot_message,
                should_respond=decision.should_respond,
                explicit=decision.explicit,
                reason=decision.reason,
            )
            if not decision.should_respond:
                debug(config, f"Ignoring channel message: {decision.reason}")
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
                explicit=decision.explicit,
            )
            return

        thread_ts = event["thread_ts"]
        thread_state = get_thread_state(config, memory, channel, thread_ts)
        decision = should_respond_to_thread_reply(
            config,
            text,
            thread_state,
            is_bot_message,
            ambient_allowed=ambient_allowed,
        )
        event_log(
            config,
            "response_decision",
            surface="thread",
            channel=channel,
            ts=event.get("ts"),
            thread_ts=thread_ts,
            text=text,
            ambient_allowed=ambient_allowed,
            is_bot_message=is_bot_message,
            thread_state=thread_state,
            should_respond=decision.should_respond,
            explicit=decision.explicit,
            reason=decision.reason,
        )
        if not decision.should_respond:
            debug(config, f"Ignoring thread reply: {decision.reason}")
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
            explicit=decision.explicit,
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
