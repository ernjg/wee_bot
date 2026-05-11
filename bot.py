import argparse
import html
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
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
    "relationships": [],
    "active_threads": {},
}

DEFAULT_ENABLED_TOOLS = (
    "get_time",
    "search_memory",
    "update_memory",
    "search_relationship_memory",
    "update_relationship_memory",
    "summarize_thread",
    "get_channel_context",
    "get_user_profile",
    "resolve_user_mention",
    "list_recent_threads",
    "save_thread_summary",
    "search_channel_history",
    "quote_recent_message",
    "get_message_permalink",
    "inspect_reactions",
    "set_reminder_note",
    "react_to_message",
    "add_reaction_set",
    "read_link_preview",
    "search_web",
    "send_voice_note",
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

KNOWN_AGENT_NAMES = (
    "wee",
    "wee marquez",
    "marquez",
    "irdnennam",
    "irdnennam seravilo",
    "irdnennam soravilo",
    "seravilo",
    "soravilo",
    "irdn",
)

URL_RE = re.compile(r"https?://[^\s<>\"]+", re.I)
MAX_ITERATIVE_TOOL_CALLS = 6
MAX_TOOL_RUNTIME_SECONDS = 300
REACTION_EMOJI_PALETTE = (
    "joy", "skull", "sob", "eyes", "thinking_face", "face_with_raised_eyebrow",
    "clap", "tada", "fire", "raised_hands", "rocket", "partying_face",
    "heart", "blue_heart", "pray", "saluting_face", "melting_face",
    "grimacing", "sweat_smile", "facepalm", "100", "ok_hand", "chefkiss",
)


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
    ambient_channel_chances: dict[str, float]
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
    reaction_response_chance: float
    ambient_reaction_fallback_chance: float
    voice_enabled: bool
    voice_provider: str
    voice_model: str
    voice_voices_path: str
    voice_python_exe: str
    voice_name: str
    voice_language: str
    voice_speed: float
    voice_format: str
    voice_max_chars: int
    voice_response_chance: float
    voice_disclosure: str
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
    explicit: bool
    tool_history: tuple[dict[str, Any], ...] = ()


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


def normalize_enabled_tools(raw_tools: Any) -> tuple[str, ...]:
    tools = list(raw_tools or DEFAULT_ENABLED_TOOLS)
    if "search_memory" in tools and "search_relationship_memory" not in tools:
        tools.insert(tools.index("search_memory") + 1, "search_relationship_memory")
    if "update_memory" in tools and "update_relationship_memory" not in tools:
        tools.insert(tools.index("update_memory") + 1, "update_relationship_memory")
    return tuple(dict.fromkeys(str(tool) for tool in tools if str(tool).strip()))


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

    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    ollama_model = (
        models.get("reply")
        or raw.get("ollama_model")
        or get_agent_env(agent_name, "OLLAMA_MODEL", "llama3.1:8b")
    )
    tool_router_model = (
        models.get("tool_router")
        or raw.get("tool_router_model")
        or get_agent_env(agent_name, "TOOL_ROUTER_MODEL", ollama_model)
    )
    voice = raw.get("voice") if isinstance(raw.get("voice"), dict) else {}
    voice_enabled_raw = voice.get("enabled", os.getenv("VOICE_ENABLED", "0"))
    voice_enabled = (
        voice_enabled_raw
        if isinstance(voice_enabled_raw, bool)
        else str(voice_enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
    )
    voice_name = voice.get("voice") or get_agent_env(agent_name, "KOKORO_VOICE") or "af_sarah"
    voice_disclosure = str(voice.get("disclosure") or "")
    voice_provider = str(voice.get("provider") or get_agent_env(agent_name, "TTS_PROVIDER", "kokoro")).strip().lower()
    ambient_channel_chances_raw = raw.get("ambient_channel_chances") if isinstance(raw.get("ambient_channel_chances"), dict) else {}
    ambient_channel_chances = {
        normalize_channel_ref(channel): max(0.0, min(float(chance), 1.0))
        for channel, chance in ambient_channel_chances_raw.items()
        if str(channel).strip()
    }

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
        ambient_channel_chances=ambient_channel_chances,
        enabled_tools=normalize_enabled_tools(raw.get("enabled_tools", DEFAULT_ENABLED_TOOLS)),
        debug=env_bool("DEBUG", True),
        event_log_enabled=env_bool("EVENT_LOG_ENABLED", False),
        event_log_path=default_event_log_path(agent_name),
        active_thread_ttl_seconds=int(raw.get("active_thread_ttl_seconds", env_int("ACTIVE_THREAD_TTL_SECONDS", 60 * 60 * 4))),
        max_active_threads=int(raw.get("max_active_threads", env_int("MAX_ACTIVE_THREADS", 25))),
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
        reaction_response_chance=max(
            0.0,
            min(
                float(raw.get("reaction_response_chance", os.getenv("REACTION_RESPONSE_CHANCE", 0.65))),
                1.0,
            ),
        ),
        ambient_reaction_fallback_chance=max(
            0.0,
            min(
                float(raw.get("ambient_reaction_fallback_chance", os.getenv("AMBIENT_REACTION_FALLBACK_CHANCE", 0.0))),
                1.0,
            ),
        ),
        voice_enabled=voice_enabled,
        voice_provider=voice_provider,
        voice_model=str(voice.get("model") or get_agent_env(agent_name, "KOKORO_MODEL_PATH", "C:/tools/kokoro/kokoro-v1.0.onnx")),
        voice_voices_path=str(voice.get("voices") or get_agent_env(agent_name, "KOKORO_VOICES_PATH", "C:/tools/kokoro/voices-v1.0.bin")),
        voice_python_exe=str(voice.get("python_exe") or get_agent_env(agent_name, "KOKORO_PYTHON_EXE", sys.executable)),
        voice_name=str(voice_name),
        voice_language=str(voice.get("language") or get_agent_env(agent_name, "KOKORO_LANGUAGE", "en-us")),
        voice_speed=max(0.25, min(float(voice.get("speed") or get_agent_env(agent_name, "KOKORO_SPEED", "1.0")), 4.0)),
        voice_format=str(voice.get("format") or os.getenv("VOICE_FORMAT") or "wav"),
        voice_max_chars=max(1, int(voice.get("max_chars") or os.getenv("VOICE_MAX_CHARS") or 600)),
        voice_response_chance=max(0.0, min(float(voice.get("response_chance") or os.getenv("VOICE_RESPONSE_CHANCE") or 0.04), 1.0)),
        voice_disclosure=str(voice_disclosure),
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
    cues = agent_address_cues(config, text)
    return text_mentions_agent(config, text) and (
        "passive_reference" in cues.reasons or "third_person_reference" in cues.reasons
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


def text_is_short_thread_followup(text: str) -> bool:
    cleaned = normalize_phrase(text)
    if not cleaned or len(cleaned) > 48:
        return False
    words = re.findall(r"[a-zA-Z0-9_']+", cleaned)
    if len(words) > 5:
        return False
    if "?" in text:
        return True
    return len(words) <= 2


def text_is_correction_or_disagreement(text: str) -> bool:
    cleaned = normalize_phrase(text)
    if "?" in text:
        return False
    correction_cues = (
        "not",
        "no",
        "wrong",
        "actually",
        "stole",
        "didn't",
        "isn't",
        "isnt",
        "wasn't",
        "wasnt",
        "don't",
        "dont",
    )
    return any(re.search(rf"\b{re.escape(cue)}\b", cleaned) for cue in correction_cues)


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


def ranked_memory_items(query: str, items: list[dict[str, Any]], text_getter, limit: int, include_recent: int = 0) -> list[dict[str, Any]]:
    now_ts = now()
    scored = []
    for index, item in enumerate(items):
        text = text_getter(item)
        if not text:
            continue
        score = relevance_score(query, text)
        if score <= 0 and include_recent <= 0:
            continue
        confidence_bonus = float(item.get("confidence", 0.7))
        use_bonus = min(int(item.get("uses", 0)), 5) * 0.1
        recency_age = max(0, now_ts - int(item.get("updated_at", 0)))
        recency_bonus = max(0.0, 1.0 - (recency_age / (90 * 24 * 60 * 60)))
        scored.append((score, confidence_bonus, use_bonus, recency_bonus, index, item))

    relevant = [
        item for score, confidence, uses, recency, index, item
        in sorted(scored, key=lambda row: (row[0], row[1], row[2], row[3], row[4]), reverse=True)
        if score > 0
    ][:limit]

    if include_recent > 0 and len(relevant) < limit:
        seen = {id(item) for item in relevant}
        recent = sorted(items, key=memory_sort_key, reverse=True)[:include_recent]
        for item in recent:
            if id(item) not in seen:
                relevant.append(item)
                seen.add(id(item))
            if len(relevant) >= limit:
                break
    return relevant[:limit]


def text_needs_time_tool(text: str) -> bool:
    cleaned = normalize_phrase(clean_slack_text(text))
    if not cleaned:
        return False

    if re.search(r"\b(what|current|exact|local)\s+(time|date|day)\b", cleaned):
        return True
    if re.search(r"\bwhat(?:'s| is)\s+today(?:'s)?\s+date\b", cleaned):
        return True
    if re.search(r"\bwhat(?:'s| is)\s+the\s+date\b", cleaned):
        return True
    if re.search(r"\bwhen\s+(was|did|is|are|were)\b", cleaned):
        return True
    if re.search(r"\bhow\s+long\s+ago\b", cleaned):
        return True
    if re.search(r"\bhow\s+long\s+(has|have|had|was|were|did)\b", cleaned):
        return True
    if re.search(r"\b(days?|hours?|minutes?|weeks?|months?|years?)\s+ago\b", cleaned):
        return True
    if re.search(r"\b(timezone|time zone)\b", cleaned):
        return True

    # Casual "now" markers like "rn", "right now", and "don't bother me now"
    # are not requests for current time.
    return False


def extract_urls(text: str) -> list[str]:
    urls = []
    for match in URL_RE.findall(text):
        url = match.rstrip(").,!?;:'\"")
        if url not in urls:
            urls.append(url)
    return urls


def text_needs_web_search(text: str) -> bool:
    cleaned = normalize_phrase(clean_slack_text(text))
    if not cleaned:
        return False
    casual_or_private = (
        "what do you think",
        "thoughts",
        "how are you",
        "what's up",
        "whats up",
        "what is up",
        "what's going on",
        "whats going on",
        "what did i miss",
        "catch me up",
        "summarize this thread",
        "search memory",
        "remember",
        "remind me",
    )
    if any(phrase in cleaned for phrase in casual_or_private):
        return False
    if re.search(r"\b(who|what|when|where|why|how)\s+(is|are|was|were|did|does|do|can|could|would|will|has|have|had|founded|started|created|invented|built|won|lost|owns|runs|leads)\b", cleaned):
        return True
    if "?" in text and re.search(r"\b(who|what|when|where|why|how)\b", cleaned):
        return True
    if re.search(r"\bwho\s+(is|are|was|were)\s+[@#]?[a-z0-9_.-]{3,}\b", cleaned):
        return True
    if re.search(r"\b(search|google|look up|lookup|find out|check online|on the web|web search)\b", cleaned):
        return True
    if re.search(r"\b(latest|current|today|recent|news|now)\b", cleaned) and re.search(r"\b(who|what|when|where|why|how|price|score|weather|status|version|release)\b", cleaned):
        return True
    return False


def text_has_slack_profile_hint(text: str) -> bool:
    cleaned_text = clean_slack_text(text)
    cleaned = normalize_phrase(cleaned_text)
    if re.search(r"@?U[A-Z0-9]{6,}", cleaned_text, flags=re.I):
        return True
    if re.search(r"\b(slack|workspace|profile|timezone|time zone|title|role|user|account)\b", cleaned):
        return True
    return False


def text_requests_links(text: str) -> bool:
    cleaned = normalize_phrase(clean_slack_text(text))
    return bool(
        re.search(r"\b(link|links|url|urls|source|sources|citation|citations|cite|where did you find|send me)\b", cleaned)
        or re.search(r"\b(can|could|please)\s+.*\b(link|source|cite)\b", cleaned)
    )


def remove_unrequested_urls(reply: str, latest_message: str) -> str:
    if text_requests_links(latest_message):
        return reply
    cleaned = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", reply)
    cleaned = re.sub(r"<https?://[^>\s]+>", "", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"\s+([.,!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def strip_reply_metadata_labels(reply: str, latest_message: str) -> str:
    cleaned = reply.strip()
    if re.search(r"(?im)^\s*(response|reply)\s*:", cleaned):
        parts = re.split(r"(?im)^\s*(?:response|reply)\s*:\s*", cleaned)
        cleaned = parts[-1].strip() if parts else cleaned

    latest = normalize_phrase(clean_slack_text(latest_message))
    kept = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if re.match(r"(?i)^(sender|user|speaker|author|content|message|latest message|text)\s*:", stripped):
            continue
        if latest and normalize_phrase(stripped).rstrip(":") == latest:
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    cleaned = re.sub(r"(?i)^\s*(response|reply)\s*:\s*", "", cleaned).strip()
    return cleaned


def strip_html_tags(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def sanitize_reply_text(config: AgentConfig, reply: str, latest_message: str, client: Any | None = None, allow_user_mentions: bool = True) -> str:
    cleaned = reply.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    cleaned = strip_reply_metadata_labels(cleaned, latest_message)
    if client is not None:
        def replace_slack_mention(match: re.Match) -> str:
            return get_user_name(config, client, match.group(1))

        if not allow_user_mentions:
            cleaned = re.sub(r"<@([UW][A-Z0-9]+)>", replace_slack_mention, cleaned)
        cleaned = re.sub(r"@([UW][A-Z0-9]{6,})\b", replace_slack_mention, cleaned)
        if not allow_user_mentions:
            cleaned = re.sub(r"\b([UW][A-Z0-9]{8,})\b", replace_slack_mention, cleaned)
    cleaned = re.sub(r"@([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+)", r"\1", cleaned)
    cleaned = re.sub(r"\bthe links (do not|don't|did not|didn't) (give|provide|show|have) (a )?(clear|direct) answer\b", "I couldn't pin down a clean answer", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(the )?search results (do not|don't|did not|didn't) (clearly )?(verify|show|say|give|provide)\b", "I couldn't verify", cleaned, flags=re.I)
    cleaned = re.sub(r"\bfrom the search results\b", "from what I found", cleaned, flags=re.I)
    cleaned = re.sub(r"\bthe links\b", "what I found", cleaned, flags=re.I)
    cleaned = remove_unrequested_urls(cleaned, latest_message)
    latest = normalize_phrase(clean_slack_text(latest_message))
    for trigger in sorted(set(config.triggers) | set(KNOWN_AGENT_NAMES), key=len, reverse=True):
        if trigger and trigger != normalize_phrase(config.display_name) and trigger in normalize_phrase(cleaned) and trigger not in latest:
            cleaned = re.sub(re.escape(trigger), "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -")
    if not cleaned or re.search(r"^\w{1,8}\s+(would|could|might|probably)\b", cleaned, flags=re.I):
        return "Yeah, fair."
    return cleaned


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


def relationship_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("text", "")).strip()
    return ""


def memory_id(text: str) -> str:
    return hashlib.sha1(normalize_phrase(text).encode("utf-8")).hexdigest()[:12]


def canonical_memory_text(text: str) -> str:
    cleaned = normalize_phrase(clean_slack_text(text))
    cleaned = re.sub(r"\b(that|this)\b", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9@#<>\s_-]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_person_name(person: str) -> str:
    cleaned = clean_slack_text(str(person)).strip().lstrip("@")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return normalize_phrase(cleaned)


def memory_sort_key(item: dict[str, Any]) -> tuple[float, int, int, int]:
    return (
        float(item.get("confidence", 0.0)),
        int(item.get("last_used", 0)),
        int(item.get("uses", 0)),
        int(item.get("updated_at", 0)),
    )


def make_memory_item(text: str, source: str, confidence: float) -> dict[str, Any]:
    timestamp = now()
    return {
        "id": memory_id(canonical_memory_text(text)),
        "text": text,
        "confidence": round(confidence, 2),
        "source": source,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_used": 0,
        "uses": 0,
    }


def make_relationship_item(text: str, people: list[str], source: str, confidence: float) -> dict[str, Any]:
    timestamp = now()
    normalized_people = sorted({normalize_person_name(person) for person in people if str(person).strip()})
    key = " | ".join(normalized_people + [canonical_memory_text(text)])
    return {
        "id": memory_id(key),
        "text": text,
        "people": normalized_people,
        "confidence": round(confidence, 2),
        "source": source,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_used": 0,
        "uses": 0,
    }


def merge_memory_item(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    if len(str(incoming.get("text", ""))) >= len(str(existing.get("text", ""))):
        existing["text"] = incoming.get("text", existing.get("text", ""))
    existing["confidence"] = round(max(float(existing.get("confidence", 0.7)), float(incoming.get("confidence", 0.7))), 2)
    existing["updated_at"] = now()
    existing["source"] = incoming.get("source") or existing.get("source", "memory")
    existing["uses"] = int(existing.get("uses", 0)) + int(incoming.get("uses", 0))
    existing["last_used"] = max(int(existing.get("last_used", 0)), int(incoming.get("last_used", 0)))


def merge_relationship_item(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    merge_memory_item(existing, incoming)
    people = {
        normalize_person_name(person)
        for person in (existing.get("people") or []) + (incoming.get("people") or [])
        if str(person).strip()
    }
    existing["people"] = sorted(people)


def upsert_memory_item(memory: dict[str, Any], item: dict[str, Any]) -> str:
    memories = memory.setdefault("memories", [])
    incoming_key = canonical_memory_text(memory_item_text(item))
    existing = next(
        (
            candidate for candidate in memories
            if candidate.get("id") == item.get("id")
            or canonical_memory_text(memory_item_text(candidate)) == incoming_key
        ),
        None,
    )
    if existing:
        merge_memory_item(existing, item)
        return "updated"
    memories.append(item)
    return "created"


def upsert_relationship_item(memory: dict[str, Any], item: dict[str, Any]) -> str:
    relationships = memory.setdefault("relationships", [])
    incoming_text = canonical_memory_text(relationship_item_text(item))
    incoming_people = set(item.get("people") or [])
    existing = next(
        (
            candidate for candidate in relationships
            if candidate.get("id") == item.get("id")
            or (
                canonical_memory_text(relationship_item_text(candidate)) == incoming_text
                and (not incoming_people or not candidate.get("people") or incoming_people & set(candidate.get("people") or []))
            )
        ),
        None,
    )
    if existing:
        merge_relationship_item(existing, item)
        return "updated"
    relationships.append(item)
    return "created"


def cleanup_memory_items(config: AgentConfig, items: list[Any], text_getter, merge_func, item_factory=make_memory_item) -> list[dict[str, Any]]:
    cutoff = now() - (config.low_confidence_memory_ttl_days * 24 * 60 * 60)
    by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in items:
        text = text_getter(item)
        if not text:
            continue
        if not isinstance(item, dict):
            item = item_factory(text, "migrated", 0.75)
        confidence = float(item.get("confidence", 0.75))
        created_at = int(item.get("created_at", now()))
        if confidence < 0.5 and created_at < cutoff:
            continue
        key = canonical_memory_text(text)
        if not key:
            continue
        if key in by_key:
            merge_func(by_key[key], item)
        else:
            by_key[key] = item
    cleaned = list(by_key.values())
    cleaned.sort(key=memory_sort_key, reverse=True)
    return cleaned[: config.max_memories]


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
    memory.setdefault("relationships", [])
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
            item["text"] = text
            item["id"] = memory_id(canonical_memory_text(text))
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

    migrated_relationships = []
    for item in memory.get("relationships", []):
        text = relationship_item_text(item)
        if not text:
            continue
        if isinstance(item, dict):
            item["text"] = text
            item["people"] = sorted({normalize_person_name(person) for person in item.get("people", []) if str(person).strip()})
            item["id"] = memory_id(" | ".join(item["people"] + [canonical_memory_text(text)]))
            item.setdefault("confidence", 0.75)
            item.setdefault("source", "relationship_file")
            item.setdefault("created_at", now())
            item.setdefault("updated_at", item["created_at"])
            item.setdefault("last_used", 0)
            item.setdefault("uses", 0)
            migrated_relationships.append(item)
        else:
            migrated_relationships.append(make_relationship_item(text, [], "migrated", 0.75))
    memory["relationships"] = migrated_relationships

    prune_memory(config, memory)
    prune_active_threads(config, memory)
    return memory


def save_memory(config: AgentConfig, memory: dict[str, Any]) -> None:
    config.memory_path.parent.mkdir(parents=True, exist_ok=True)
    prune_memory(config, memory)
    prune_active_threads(config, memory)
    tmp_path = config.memory_path.with_suffix(".tmp.json")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)
    tmp_path.replace(config.memory_path)


def prune_memory(config: AgentConfig, memory: dict[str, Any]) -> None:
    memory["memories"] = cleanup_memory_items(
        config,
        memory.get("memories", []),
        memory_item_text,
        merge_memory_item,
    )
    memory["relationships"] = cleanup_memory_items(
        config,
        memory.get("relationships", []),
        relationship_item_text,
        merge_relationship_item,
        item_factory=lambda text, source, confidence: make_relationship_item(text, [], source, confidence),
    )


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
    if config.bot_user_id and user_id == config.bot_user_id:
        return config.display_name
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
            debug(config, f"Channel name lookup disabled for explicit-only channel checks; add channels:read or use channel IDs in explicit_only_channels. channel={channel_id}")
        else:
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


def configured_channel_chance(config: AgentConfig, client, channel_id: str) -> float | None:
    if not config.ambient_channel_chances:
        return None
    normalized_id = normalize_channel_ref(channel_id)
    if normalized_id in config.ambient_channel_chances:
        return config.ambient_channel_chances[normalized_id]
    channel_name = get_channel_name(config, client, channel_id)
    if channel_name and channel_name in config.ambient_channel_chances:
        return config.ambient_channel_chances[channel_name]
    return None


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
    action = upsert_memory_item(memory, item)

    prune_memory(config, memory)
    save_memory(config, memory)
    if confidence < 0.5:
        return f"I'll keep that as a low-confidence memory: {fact}"
    return f"Got it. I'll {action} that memory: {fact}"


def parse_relationship_people(text: str) -> list[str]:
    mentions = re.findall(r"<@([UW][A-Z0-9]+)>", text)
    people = [f"<@{mention}>" for mention in mentions]
    for left, right in re.findall(r"\b([A-Z][A-Za-z]+)\s+(?:and|&|\+|with|vs\.?|versus)\s+([A-Z][A-Za-z]+)\b", text):
        people.extend([left, right])
    for possessive in re.findall(r"\b([A-Z][A-Za-z]+)'s\b", text):
        people.append(possessive)
    return sorted({person for person in people if person.strip()})


def maybe_update_relationship_memory(config: AgentConfig, text: str, memory: dict[str, Any], speaker_name: str) -> str | None:
    fact = find_memory_command_payload(
        text,
        (
            "remember relationship that",
            "remember relationship this",
            "remember relationship:",
            "remember relation that",
            "remember relation this",
            "remember relation:",
            "relationship memory:",
            "relationship:",
        ),
    )
    if fact is None:
        return None
    if not fact:
        return "I can remember a relationship, but you have to tell me what."

    confidence = memory_confidence(fact)
    people = parse_relationship_people(fact)
    item = make_relationship_item(fact, people, speaker_name, confidence)
    action = upsert_relationship_item(memory, item)
    prune_memory(config, memory)
    save_memory(config, memory)
    people_text = f" ({', '.join(item.get('people') or [])})" if item.get("people") else ""
    if confidence < 0.5:
        return f"I'll keep that as a low-confidence relationship memory{people_text}: {fact}"
    return f"Got it. I'll {action} that relationship memory{people_text}: {fact}"


def maybe_forget_memory(config: AgentConfig, text: str, memory: dict[str, Any]) -> str | None:
    relationship_target = find_memory_command_payload(
        text,
        ("forget relationship that", "forget relationship this", "forget relationship:", "forget relation:", "forget relationship memory:"),
    )
    if relationship_target is not None:
        if not relationship_target:
            return "Tell me which relationship memory to forget."
        old_relationships = memory.get("relationships", [])
        normalized_target = normalize_phrase(relationship_target)
        if normalized_target in {"everything", "all", "all relationships", "all relationship memories"}:
            count = len(old_relationships)
            memory["relationships"] = []
            save_memory(config, memory)
            return f"Forgot {count} relationship memories."
        new_relationships = [
            item for item in old_relationships
            if normalized_target not in normalize_phrase(relationship_item_text(item) + " " + " ".join(item.get("people", [])))
        ]
        if len(new_relationships) == len(old_relationships):
            return "I couldn't find a matching relationship memory."
        memory["relationships"] = new_relationships
        save_memory(config, memory)
        return f"Forgot {len(old_relationships) - len(new_relationships)} matching relationship memory."

    target = find_memory_command_payload(text, ("forget that", "forget this", "forget:"))
    if target is None:
        return None
    if not target:
        return "Tell me what to forget."
    if normalize_phrase(target) in {"everything", "all", "all memories"}:
        count = len(memory.get("memories", []))
        relationship_count = len(memory.get("relationships", []))
        memory["memories"] = []
        memory["relationships"] = []
        save_memory(config, memory)
        return f"Forgot {count} memories and {relationship_count} relationship memories."

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
    relationship_triggers = [
        "show relationship memory",
        "show relationship memories",
        "what relationship memories",
        "what relationships do you remember",
    ]
    if any(t in cleaned for t in relationship_triggers):
        relationships = memory.get("relationships", [])
        if not relationships:
            return "I don't have any relationship memories yet."
        recent_relationships = sorted(relationships, key=memory_sort_key, reverse=True)[:20]
        lines = "\n".join(
            f"- {relationship_item_text(item)}"
            + (f" [people: {', '.join(item.get('people') or [])}]" if item.get("people") else "")
            + f" (confidence {float(item.get('confidence', 0.7)):.2f})"
            for item in recent_relationships
        )
        return f"Here are the relationship memories I have:\n{lines}"

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
    relationships = memory.get("relationships", [])
    if not memories and not relationships:
        return "I don't remember anything yet."
    recent = sorted(memories, key=memory_sort_key, reverse=True)[:20]
    memory_lines = "\n".join(
        f"- {memory_item_text(item)} (confidence {float(item.get('confidence', 0.7)):.2f})"
        for item in recent
    )
    relationship_lines = "\n".join(
        f"- {relationship_item_text(item)}"
        + (f" [people: {', '.join(item.get('people') or [])}]" if item.get("people") else "")
        + f" (confidence {float(item.get('confidence', 0.7)):.2f})"
        for item in sorted(relationships, key=memory_sort_key, reverse=True)[:10]
    )
    sections = []
    if memory_lines:
        sections.append("Memories:\n" + memory_lines)
    if relationship_lines:
        sections.append("Relationship memories:\n" + relationship_lines)
    return "Here's what I remember:\n" + "\n\n".join(sections)


def handle_memory_commands(config: AgentConfig, text: str, memory: dict[str, Any], speaker_name: str) -> str | None:
    return (
        maybe_show_memory(config, text, memory)
        or maybe_forget_memory(config, text, memory)
        or maybe_update_relationship_memory(config, text, memory, speaker_name)
        or maybe_update_memory(config, text, memory, speaker_name)
    )


def strip_agent_address_for_memory(config: AgentConfig, text: str) -> str:
    cleaned = clean_slack_text(text)
    trigger_pattern = agent_trigger_pattern(config)
    if trigger_pattern:
        cleaned = re.sub(rf"^\s*(hey|hi|hello|yo|ok|okay)?\s*({trigger_pattern})\b\s*[,:\-]?\s*", "", cleaned, flags=re.I)
    for trigger in sorted(config.triggers, key=len, reverse=True):
        if trigger.startswith("@"):
            cleaned = re.sub(rf"^\s*{re.escape(trigger)}\s*[,:\-]?\s*", "", cleaned, flags=re.I)
    return cleaned.strip()


def text_may_contain_auto_memory(config: AgentConfig, text: str) -> bool:
    cleaned = strip_agent_address_for_memory(config, text)
    normalized = normalize_phrase(cleaned)
    if not normalized or "?" in cleaned:
        return False
    if len(re.findall(r"[a-zA-Z0-9_']+", normalized)) < 3:
        return False
    if any(
        cue in normalized
        for cue in (
            "show memory",
            "show memories",
            "search memory",
            "forget",
            "remind me",
            "what did i miss",
            "summarize",
            "look up",
            "google",
            "search web",
            "link preview",
            "voice note",
            "what time",
            "how long",
        )
    ):
        return False
    if normalized.startswith(("remember ", "react ", "please react", "send a voice note")):
        return False
    durable_patterns = (
        r"\b(my|his|her|their|our|your)\s+[\w\s]{1,40}\s+(is|are|was|were)\b",
        r"\b(i|we|he|she|they|[A-Za-z][A-Za-z0-9_-]{1,30})\s+(like|likes|love|loves|hate|hates|prefer|prefers|work|works|live|lives|have|has|own|owns|want|wants|need|needs|use|uses|enjoy|enjoys)\b",
        r"\b[A-Za-z][A-Za-z0-9_-]{1,30}(?:'s)?\s+[\w\s]{0,40}\s+(is|are|was|were)\s+[\w'-]{2,}\b",
        r"\b[A-Z][A-Za-z]+(?:'s)?\s+[\w\s]{1,40}\s+(is|are|was|were|likes|loves|hates|prefers|works|lives|has)\b",
        r"\b[A-Za-z][A-Za-z0-9_-]{1,30}\s+(and|with|vs\.?|versus)\s+[A-Za-z][A-Za-z0-9_-]{1,30}\b",
        r"\b[A-Za-z][A-Za-z0-9_-]{1,30}\s+(is|are|was|were)\s+(friends|best friends|brothers|roommates|rivals|enemies|dating|flaky|flakey|reliable|unreliable)\b",
    )
    return any(re.search(pattern, cleaned) for pattern in durable_patterns)


def build_auto_memory_prompt(config: AgentConfig, text: str, speaker_name: str) -> str:
    cleaned = strip_agent_address_for_memory(config, text)
    return f"""
Automatic memory extraction for a Slack chat agent.

Speaker: {speaker_name}
Message: {cleaned}

Extract only durable facts that would help future replies.
Save ordinary facts as memories. Save interpersonal/social context as relationship memories.

Rules:
- Do not save questions, commands, one-off plans, temporary status, reminders, or current task instructions.
- Do save recurring jokes, nicknames, preferences, interpersonal dynamics, and opinions about people when they would help future chat feel more aware.
- Do not save broad public facts unless the fact is about a workspace person or this chat.
- Convert first-person claims into third person using the speaker name.
- Keep each memory as one concise sentence.
- Relationship memories should include people names when clear.
- If nothing should be saved, return empty arrays.
- Return only JSON.

Schema:
{{
  "memories": [
    {{"text": "Speaker's favorite color is green.", "confidence": 0.7}}
  ],
  "relationships": [
    {{"text": "Ernesto and Andre have a friendly poker rivalry.", "people": ["Ernesto", "Andre"], "confidence": 0.7}}
  ]
}}
""".strip()


def auto_capture_memory(config: AgentConfig, text: str, memory: dict[str, Any], speaker_name: str) -> dict[str, int]:
    if not text_may_contain_auto_memory(config, text):
        return {"memories": 0, "relationships": 0}
    raw = call_ollama(
        config,
        build_auto_memory_prompt(config, text, speaker_name),
        model=config.tool_router_model,
        num_predict=260,
        temperature=0.0,
    )
    parsed = extract_json_object(raw)
    if not isinstance(parsed, dict):
        event_log(config, "auto_memory_extract_failed", reason="invalid_json", raw=raw[:1000])
        return {"memories": 0, "relationships": 0}

    saved_memories = 0
    saved_relationships = 0
    for entry in parsed.get("memories") or []:
        if not isinstance(entry, dict):
            continue
        fact = str(entry.get("text") or "").strip()
        if not fact:
            continue
        confidence = max(0.0, min(float(entry.get("confidence") or memory_confidence(fact)), 1.0))
        if confidence < 0.4:
            continue
        upsert_memory_item(memory, make_memory_item(fact, speaker_name, confidence))
        saved_memories += 1

    for entry in parsed.get("relationships") or []:
        if not isinstance(entry, dict):
            continue
        fact = str(entry.get("text") or "").strip()
        if not fact:
            continue
        raw_people = entry.get("people") or parse_relationship_people(fact)
        if isinstance(raw_people, str):
            raw_people = re.split(r"[,;&]|\band\b", raw_people)
        if not isinstance(raw_people, list):
            raw_people = []
        people = [normalize_person_name(str(person)) for person in raw_people if str(person).strip()]
        confidence = max(0.0, min(float(entry.get("confidence") or memory_confidence(fact)), 1.0))
        if confidence < 0.4:
            continue
        upsert_relationship_item(memory, make_relationship_item(fact, people, speaker_name, confidence))
        saved_relationships += 1

    if saved_memories or saved_relationships:
        prune_memory(config, memory)
        save_memory(config, memory)
        event_log(
            config,
            "auto_memory_saved",
            text=text,
            memories=saved_memories,
            relationships=saved_relationships,
        )
    return {"memories": saved_memories, "relationships": saved_relationships}


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
    matches = ranked_memory_items(query, memories, memory_item_text, limit)
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
    action = upsert_memory_item(ctx.memory, item)

    prune_memory(ctx.config, ctx.memory)
    save_memory(ctx.config, ctx.memory)
    return {"saved": True, "action": action, "text": fact, "confidence": confidence}


def tool_search_relationship_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or ctx.latest_message)
    limit = max(1, min(int(args.get("limit") or ctx.config.max_memory_lines), 10))
    relationships = [
        item for item in ctx.memory.get("relationships", [])
        if float(item.get("confidence", 0.7)) >= 0.4
    ]
    matches = ranked_memory_items(
        query,
        relationships,
        lambda item: relationship_item_text(item) + " " + " ".join(item.get("people", [])),
        limit,
    )
    return {
        "query": query,
        "matches": [
            {
                "text": relationship_item_text(item),
                "people": item.get("people", []),
                "confidence": float(item.get("confidence", 0.7)),
                "source": item.get("source", "relationship_memory"),
            }
            for item in matches
        ],
    }


def tool_update_relationship_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    text = str(args.get("text") or args.get("fact") or "").strip()
    if not text:
        return {"saved": False, "error": "Missing relationship text."}
    raw_people = args.get("people") or args.get("users") or []
    if isinstance(raw_people, str):
        raw_people = re.split(r"[,;&]|\band\b", raw_people)
    if not isinstance(raw_people, list):
        raw_people = []
    people = [normalize_person_name(str(person)) for person in raw_people if str(person).strip()]
    if not people:
        people = [normalize_person_name(person) for person in parse_relationship_people(text)]
    confidence = max(0.0, min(float(args.get("confidence") or memory_confidence(text)), 1.0))
    item = make_relationship_item(text, people, ctx.speaker_name, confidence)
    action = upsert_relationship_item(ctx.memory, item)
    prune_memory(ctx.config, ctx.memory)
    save_memory(ctx.config, ctx.memory)
    return {"saved": True, "action": action, "text": text, "people": item["people"], "confidence": confidence}


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
    limit = max(1, min(int(args.get("limit") or max(ctx.config.channel_fetch_limit, 60)), 100))
    lines = ctx.channel_context_lines
    if len(lines) < limit:
        try:
            result = ctx.client.conversations_history(
                channel=ctx.channel,
                latest=ctx.latest_ts,
                limit=limit,
                inclusive=False,
            )
            lines = slack_messages_to_lines(ctx.config, ctx.client, list(reversed(result.get("messages", []))))
        except Exception as e:
            debug(ctx.config, f"Could not fetch extended channel context: {e}")
    lines = lines[-limit:]
    if not lines:
        return {
            "lines": [],
            "summary": "I don't see much recent channel context.",
            "note": "Most recent channel messages before the latest message.",
        }

    prompt = f"""
Summarize what happened recently in this Slack channel in a natural, concise way.
Use only the messages below. Prioritize concrete events, decisions, asks, tool tests, and active topics.
Do not mention every line. Do not invent missing details.

Recent channel messages:
{chr(10).join(lines)}
""".strip()
    summary = call_ollama(ctx.config, prompt, num_predict=180, temperature=0.2)
    return {
        "lines": lines,
        "summary": summary,
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


def find_slack_users(config: AgentConfig, client, query: str, limit: int = 5) -> list[dict[str, Any]]:
    target = normalize_phrase(clean_slack_text(query)).lstrip("@")
    if not target:
        return []
    user_id_match = re.search(r"@?(U[A-Z0-9]+)", query, flags=re.I)
    if user_id_match:
        user_id = user_id_match.group(1).upper()
        try:
            user = client.users_info(user=user_id).get("user", {})
            profile = user.get("profile", {})
            return [{
                "id": user.get("id"),
                "name": profile.get("display_name") or profile.get("real_name") or user.get("name"),
                "real_name": profile.get("real_name"),
            }]
        except Exception:
            return []
    try:
        result = client.users_list(limit=200)
    except Exception:
        return []
    matches = []
    for user in result.get("members", []):
        profile = user.get("profile", {})
        names = [user.get("name", ""), profile.get("display_name", ""), profile.get("real_name", "")]
        if any(target and target in normalize_phrase(name) for name in names):
            matches.append({
                "id": user.get("id"),
                "name": profile.get("display_name") or profile.get("real_name") or user.get("name"),
                "real_name": profile.get("real_name"),
            })
            if len(matches) >= limit:
                break
    return matches


def tool_resolve_user_mention(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("user") or args.get("name") or "").strip()
    if not query:
        return {"found": False, "error": "Missing user."}
    matches = find_slack_users(ctx.config, ctx.client, query, limit=5)
    if not matches:
        return {"found": False, "query": query, "matches": []}
    if len(matches) > 1:
        return {
            "found": True,
            "ambiguous": True,
            "query": query,
            "matches": [{**match, "mention": f"<@{match.get('id')}>" if match.get("id") else ""} for match in matches],
        }
    user = matches[0]
    mention = f"<@{user.get('id')}>" if user.get("id") else ""
    return {"found": True, "ambiguous": False, "query": query, **user, "mention": mention}


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
        (relevance_score(query, line), index, line)
        for index, line in enumerate(messages)
    ]
    matches = [
        line for score, _, line in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)
        if score > 0
    ][:limit]
    return {"query": query, "matches": matches}


def message_summary(config: AgentConfig, client, msg: dict[str, Any]) -> dict[str, Any]:
    speaker = get_event_speaker_name(config, client, msg)
    ts = str(msg.get("ts") or "")
    return {
        "ts": ts,
        "thread_ts": msg.get("thread_ts"),
        "time": format_slack_timestamp(ts),
        "speaker": speaker,
        "text": clean_slack_text(msg.get("text", ""))[:500],
        "reply_count": int(msg.get("reply_count", 0) or 0),
    }


def tool_quote_recent_message(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or args.get("topic") or "").strip()
    speaker_query = normalize_phrase(str(args.get("speaker") or args.get("user") or ""))
    limit = max(1, min(int(args.get("limit") or ctx.config.channel_fetch_limit), 100))
    try:
        result = ctx.client.conversations_history(
            channel=ctx.channel,
            latest=ctx.latest_ts,
            limit=limit,
            inclusive=False,
        )
    except Exception as e:
        return {"found": False, "error": str(e), "query": query}

    candidates = []
    for index, msg in enumerate(result.get("messages", [])):
        text = clean_slack_text(msg.get("text", ""))
        if not text:
            continue
        speaker = get_event_speaker_name(ctx.config, ctx.client, msg)
        if speaker_query and speaker_query not in normalize_phrase(speaker):
            continue
        score = relevance_score(query, text) if query else 1
        if score <= 0:
            continue
        candidates.append((score, -index, message_summary(ctx.config, ctx.client, msg)))

    if not candidates:
        return {"found": False, "query": query, "speaker": speaker_query, "matches": []}
    matches = [item for _, _, item in sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)[:3]]
    return {"found": True, "query": query, "speaker": speaker_query, "matches": matches}


def tool_get_message_permalink(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target_ts = str(args.get("ts") or ctx.thread_ts or ctx.latest_ts)
    try:
        result = ctx.client.chat_getPermalink(channel=ctx.channel, message_ts=target_ts)
    except Exception as e:
        return {"found": False, "ts": target_ts, "error": str(e)}
    return {
        "found": bool(result.get("ok", True) and result.get("permalink")),
        "ts": target_ts,
        "permalink": result.get("permalink"),
    }


def tool_inspect_reactions(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target_ts = str(args.get("ts") or ctx.latest_ts)
    try:
        result = ctx.client.reactions_get(channel=ctx.channel, timestamp=target_ts, full=True)
    except Exception as e:
        return {"found": False, "ts": target_ts, "error": str(e)}
    message = result.get("message", {})
    reactions = []
    for reaction in message.get("reactions", []):
        users = reaction.get("users") or []
        reactions.append({
            "name": reaction.get("name"),
            "count": int(reaction.get("count", len(users)) or 0),
            "users": [get_user_name(ctx.config, ctx.client, user) for user in users[:10]],
        })
    return {
        "found": True,
        "ts": target_ts,
        "text": clean_slack_text(message.get("text", ""))[:500],
        "reactions": reactions,
    }


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


def synthesize_voice_note(config: AgentConfig, text: str) -> Path:
    output_format = config.voice_format.strip(".").lower()
    suffix = "." + output_format
    repo_root = Path(__file__).resolve().parent
    temp_dir = repo_root / "logs" / "voice"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=temp_dir)
    path = Path(temp.name)
    temp.close()

    if config.voice_provider == "fake":
        path.write_bytes(b"fake audio")
        return path

    if config.voice_provider != "kokoro":
        raise RuntimeError(f"Unsupported TTS provider: {config.voice_provider}")

    model_path = Path(config.voice_model)
    voices_path = Path(config.voice_voices_path)
    if not model_path.exists():
        raise RuntimeError(f"Missing Kokoro model file: {model_path}")
    if not voices_path.exists():
        raise RuntimeError(f"Missing Kokoro voices file: {voices_path}")

    helper_path = Path(__file__).resolve().parent / "scripts" / "kokoro_synthesize.py"
    python_exe = Path(config.voice_python_exe)
    if not python_exe.is_absolute():
        python_exe = repo_root / python_exe
    command = [
        str(python_exe),
        str(helper_path),
        "--model",
        str(model_path),
        "--voices",
        str(voices_path),
        "--voice",
        config.voice_name,
        "--language",
        config.voice_language,
        "--speed",
        str(config.voice_speed),
        "--format",
        output_format,
        "--output",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            input=text,
            text=True,
            capture_output=True,
            timeout=config.ollama_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            event_log(config, "voice_synthesis_failed", provider=config.voice_provider, error=detail[:1000])
            raise RuntimeError(f"Kokoro TTS failed with exit code {completed.returncode}: {detail[:500]}")
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError("Kokoro TTS did not create an audio file.")
        return path
    except Exception:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        raise


def upload_voice_note(ctx: ToolContext, path: Path, text: str) -> dict[str, Any]:
    filename = f"{ctx.config.name}-voice-note{path.suffix}"
    upload_args = {
        "channel": ctx.channel,
        "file": str(path),
        "filename": filename,
        "title": f"{ctx.config.display_name} voice note",
        "thread_ts": ctx.thread_ts,
    }
    if ctx.config.voice_provider == "fake":
        upload_args["_voice_text"] = text
    if ctx.config.voice_disclosure:
        upload_args["initial_comment"] = ctx.config.voice_disclosure
    result = ctx.client.files_upload_v2(**upload_args)
    if not result.get("ok", True):
        raise RuntimeError(f"Slack rejected voice upload: {result.get('error', 'unknown_error')}")
    return {
        "uploaded": bool(result.get("ok", True)),
        "file": result.get("file") or (result.get("files") or [{}])[0],
        "text": text,
    }


def prepare_tts_text(text: str, max_chars: int, *, ascii_only: bool = False) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"<https?://[^|>]+(?:\|([^>]+))?>", r"\1", cleaned)
    cleaned = re.sub(r"<@([A-Z0-9]+)>", r"user \1", cleaned)
    cleaned = re.sub(r"@([A-Za-z][A-Za-z0-9_.-]*)", r"\1", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"[*_`~]", "", cleaned)
    cleaned = re.sub(r":([a-zA-Z0-9_+-]+):", "", cleaned)
    cleaned = cleaned.translate(str.maketrans({
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
    }))
    if ascii_only:
        cleaned = unicodedata.normalize("NFKD", cleaned).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned

    truncated = cleaned[:max_chars].rstrip()
    sentence_end = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if sentence_end >= max_chars * 0.65:
        return truncated[: sentence_end + 1].strip()
    word_end = truncated.rfind(" ")
    if word_end > 0:
        return truncated[:word_end].strip()
    return truncated


def send_voice_note_text(ctx: ToolContext, text: str, *, fallback_to_text: bool) -> dict[str, Any]:
    text = prepare_tts_text(text, ctx.config.voice_max_chars)
    if not text:
        return {
            "uploaded": False,
            "error": "No voice note text generated.",
            "reply_text": "I couldn't generate a voice note for that.",
        }

    path: Path | None = None
    try:
        try:
            path = synthesize_voice_note(ctx.config, text)
        except Exception as first_error:
            retry_text = prepare_tts_text(text, ctx.config.voice_max_chars, ascii_only=True)
            if retry_text and retry_text != text:
                debug(ctx.config, f"Voice synthesis retrying with ASCII-cleaned text after: {first_error}")
                path = synthesize_voice_note(ctx.config, retry_text)
                text = retry_text
            else:
                raise
        result = upload_voice_note(ctx, path, text)
        result["reply_text"] = ""
        debug(ctx.config, f"Voice note uploaded with {ctx.config.voice_provider}: {result.get('file', {}).get('id', 'unknown_file')}")
        return result
    except Exception as e:
        debug(ctx.config, f"Voice note failed with {ctx.config.voice_provider}: {e}")
        event_log(ctx.config, "voice_note_failed", provider=ctx.config.voice_provider, error=str(e), text=text)
        return {
            "uploaded": False,
            "error": str(e),
            "text": text,
            "reply_text": (
                f"I couldn't send a voice note, but here's what I would have said: {text}"
                if fallback_to_text
                else "I hit a voice-note issue on my end. I logged the error so we can debug it."
            ),
        }
    finally:
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def tool_send_voice_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.config.voice_enabled:
        return {
            "uploaded": False,
            "error": "Voice notes are disabled for this agent.",
            "reply_text": "Voice notes are disabled for me right now.",
        }

    reply = call_ollama(
        ctx.config,
        build_voice_prompt(
            config=ctx.config,
            latest_message=ctx.latest_message,
            speaker_name=ctx.speaker_name,
            memory=ctx.memory,
            thread_context_lines=ctx.thread_context_lines,
            channel_context_lines=ctx.channel_context_lines,
        ),
        num_predict=360,
        temperature=0.75,
    )
    return send_voice_note_text(ctx, reply, fallback_to_text=False)


def maybe_send_voice_instead(ctx: ToolContext, reply: str) -> str:
    reply = sanitize_reply_text(ctx.config, reply, ctx.latest_message, ctx.client)
    if not reply or not ctx.config.voice_enabled or ctx.config.voice_response_chance <= 0:
        return reply
    if not deterministic_chance(f"voice:{ctx.channel}:{ctx.latest_ts}:{reply}", ctx.config.voice_response_chance):
        return reply
    result = send_voice_note_text(ctx, reply, fallback_to_text=True)
    return str(result.get("reply_text") or "").strip()


def contextual_reaction_emoji(text: str, salt: str = "") -> str:
    cleaned = normalize_phrase(text)
    if any(word in cleaned for word in ["ship", "shipped", "win", "done", "launch", "passed", "fixed", "merged"]):
        options = ("tada", "rocket", "fire", "raised_hands", "partying_face")
    elif any(word in cleaned for word in ["lol", "lmao", "haha", "funny", "joke", "wild"]):
        options = ("joy", "skull", "sob", "sweat_smile")
    elif any(word in cleaned for word in ["hmm", "weird", "interesting", "curious", "maybe", "why"]):
        options = ("thinking_face", "eyes", "face_with_raised_eyebrow")
    elif any(word in cleaned for word in ["rip", "bad", "broken", "failed", "issue", "problem"]):
        options = ("grimacing", "facepalm", "melting_face", "skull")
    elif any(word in cleaned for word in ["thanks", "thank you", "ty", "appreciate"]):
        options = ("pray", "heart", "blue_heart", "saluting_face")
    else:
        options = REACTION_EMOJI_PALETTE
    digest = hashlib.sha1(f"{cleaned}:{salt}".encode("utf-8")).hexdigest()[:8]
    return options[int(digest, 16) % len(options)]


def tool_react_to_message(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    emoji = str(args.get("emoji") or "").strip().strip(":")
    if not ctx.explicit and emoji in {"", "thumbsup", "+1", "white_check_mark"}:
        emoji = contextual_reaction_emoji(ctx.latest_message, ctx.latest_ts)
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


def add_single_reaction(ctx: ToolContext, emoji: str, target_ts: str) -> dict[str, Any]:
    emoji = emoji.strip().strip(":")
    if not emoji:
        return {"reacted": False, "error": "Missing emoji.", "ts": target_ts}
    try:
        result = ctx.client.reactions_get(channel=ctx.channel, timestamp=target_ts, full=False)
        message = result.get("message", {})
        for reaction in message.get("reactions", []):
            if reaction.get("name") == emoji:
                return {"reacted": False, "emoji": emoji, "reason": "already_present", "ts": target_ts}
    except Exception as e:
        debug(ctx.config, f"Could not inspect existing reactions: {e}")
    try:
        ctx.client.reactions_add(channel=ctx.channel, timestamp=target_ts, name=emoji)
    except Exception as e:
        return {"reacted": False, "emoji": emoji, "error": str(e), "ts": target_ts}
    return {"reacted": True, "emoji": emoji, "ts": target_ts}


def tool_add_reaction_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_emojis = args.get("emojis") or args.get("emoji") or []
    if isinstance(raw_emojis, str):
        raw_emojis = re.split(r"[\s,]+", raw_emojis)
    if not isinstance(raw_emojis, list):
        return {"reacted": False, "error": "Missing emojis.", "reply_text": ""}
    emojis = []
    for emoji in raw_emojis:
        cleaned = str(emoji).strip().strip(":")
        if cleaned and cleaned not in emojis:
            emojis.append(cleaned)
    if not ctx.explicit:
        generic = {"thumbsup", "+1", "white_check_mark"}
        emojis = [
            contextual_reaction_emoji(ctx.latest_message, f"{ctx.latest_ts}:{index}") if emoji in generic else emoji
            for index, emoji in enumerate(emojis)
        ]
        emojis = list(dict.fromkeys(emojis))
    emojis = emojis[:3]
    if not emojis:
        return {"reacted": False, "error": "Missing emojis.", "reply_text": ""}
    target_ts = str(args.get("ts") or ctx.latest_ts)
    results = [add_single_reaction(ctx, emoji, target_ts) for emoji in emojis]
    return {
        "reacted": any(result.get("reacted") for result in results),
        "emojis": emojis,
        "results": results,
        "ts": target_ts,
        "reply_text": "",
    }


def tool_read_link_preview(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    if not url:
        urls = extract_urls(ctx.latest_message)
        url = urls[0] if urls else ""
    if not url:
        return {"found": False, "error": "No URL found."}
    if not url.lower().startswith(("http://", "https://")):
        return {"found": False, "url": url, "error": "Unsupported URL."}
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "slack-fun-agent/1.0 (+link-preview)"},
            timeout=min(ctx.config.ollama_timeout_seconds, 15),
            allow_redirects=True,
        )
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400:
            return {"found": False, "url": url, "status": response.status_code, "error": "HTTP error."}
        body = response.text[:200000]
    except Exception as e:
        return {"found": False, "url": url, "error": str(e)}

    title = ""
    description = ""
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    if title_match:
        title = strip_html_tags(title_match.group(1))
    desc_match = re.search(r'(?is)<meta[^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\'][^>]+content=["\'](.*?)["\']', body)
    if not desc_match:
        desc_match = re.search(r'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\']', body)
    if desc_match:
        description = strip_html_tags(desc_match.group(1))
    text = strip_html_tags(body)
    if title and text.startswith(title):
        text = text[len(title):].strip()
    return {
        "found": True,
        "url": response.url,
        "status": response.status_code,
        "content_type": content_type,
        "title": title[:300],
        "description": description[:800],
        "excerpt": text[:1200],
    }


def tool_search_web(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip() or clean_slack_text(ctx.latest_message)
    if not query:
        return {"query": query, "results": [], "error": "Missing search query."}
    try:
        response = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "slack-fun-agent/1.0 (+web-search)"},
            timeout=min(ctx.config.ollama_timeout_seconds, 15),
        )
        if response.status_code >= 400:
            return {"query": query, "results": [], "status": response.status_code, "error": "Search HTTP error."}
        body = response.text
    except Exception as e:
        return {"query": query, "results": [], "error": str(e)}

    results = []
    for match in re.finditer(r'(?is)<a[^>]+class=["\']result__a["\'][^>]+href=["\'](.*?)["\'][^>]*>(.*?)</a>', body):
        href = html.unescape(match.group(1))
        title = strip_html_tags(match.group(2))
        tail = body[match.end(): match.end() + 2500]
        snippet_match = re.search(r'(?is)<a[^>]+class=["\']result__snippet["\'][^>]*>(.*?)</a>', tail)
        snippet = strip_html_tags(snippet_match.group(1)) if snippet_match else ""
        results.append({"title": title[:200], "url": href, "snippet": snippet[:500]})
        if len(results) >= 5:
            break
    return {"query": query, "results": results}


TOOL_HANDLERS = {
    "get_time": tool_get_time,
    "search_memory": tool_search_memory,
    "update_memory": tool_update_memory,
    "search_relationship_memory": tool_search_relationship_memory,
    "update_relationship_memory": tool_update_relationship_memory,
    "summarize_thread": tool_summarize_thread,
    "get_channel_context": tool_get_channel_context,
    "get_user_profile": tool_get_user_profile,
    "list_recent_threads": tool_list_recent_threads,
    "save_thread_summary": tool_save_thread_summary,
    "search_channel_history": tool_search_channel_history,
    "quote_recent_message": tool_quote_recent_message,
    "get_message_permalink": tool_get_message_permalink,
    "inspect_reactions": tool_inspect_reactions,
    "set_reminder_note": tool_set_reminder_note,
    "react_to_message": tool_react_to_message,
    "add_reaction_set": tool_add_reaction_set,
    "read_link_preview": tool_read_link_preview,
    "search_web": tool_search_web,
    "send_voice_note": tool_send_voice_note,
}


TOOL_DESCRIPTIONS = {
    "get_time": "Get the current date/time for time, date, elapsed-time, or relative-time questions. Arguments: timezone, optional IANA timezone.",
    "search_memory": "Search this agent's memory. Arguments: query, optional limit.",
    "update_memory": "Save one durable memory claim. Arguments: text or fact, optional confidence 0-1.",
    "search_relationship_memory": "Search this agent's relationship memories about people and social context. Arguments: query, optional limit.",
    "update_relationship_memory": "Save one durable relationship memory. Arguments: text or fact, optional people/users list, optional confidence 0-1.",
    "summarize_thread": "Summarize the current Slack thread. Arguments: optional focus.",
    "get_channel_context": "Inspect recent channel messages before this event. Arguments: optional limit.",
    "get_user_profile": "Look up a Slack user's basic profile. Arguments: user/name.",
    "list_recent_threads": "List recently active threads in the current channel. Arguments: optional limit.",
    "save_thread_summary": "Save a durable memory summary of the current thread. Arguments: optional summary/confidence.",
    "search_channel_history": "Search recent messages in the current channel. Arguments: query, optional limit.",
    "quote_recent_message": "Find and quote recent channel message(s) by topic or speaker. Arguments: query, optional speaker/user, optional limit.",
    "get_message_permalink": "Get a Slack permalink for the latest, thread root, or specified message. Arguments: optional ts.",
    "inspect_reactions": "Inspect emoji reactions on the latest or specified Slack message. Arguments: optional ts.",
    "set_reminder_note": "Save a local reminder note with optional due text. Arguments: text/note, optional due/when.",
    "react_to_message": "Add a natural emoji reaction to the current or specified message. Arguments: emoji, optional ts.",
    "add_reaction_set": "Add 1-3 natural emoji reactions to the current or specified message. Arguments: emojis, optional ts.",
    "read_link_preview": "Fetch title, description, and a short excerpt from a URL in the latest message. Arguments: url.",
    "search_web": "Search the public web for current information when Slack context and memory are insufficient. Arguments: query.",
    "send_voice_note": "Generate a spoken TTS audio response and upload it to Slack as a playable voice note. Arguments: optional text.",
}


def selected_memory_lines(config: AgentConfig, latest_message: str, memory: dict[str, Any]) -> list[str]:
    memories = [
        item for item in memory.get("memories", [])
        if float(item.get("confidence", 0.7)) >= 0.4
    ]
    by_relevance = ranked_memory_items(
        latest_message,
        memories,
        memory_item_text,
        config.max_memory_lines,
        include_recent=min(2, config.max_memory_lines),
    )
    selected = []
    for item in by_relevance:
        text = memory_item_text(item)
        item["last_used"] = now()
        item["uses"] = int(item.get("uses", 0)) + 1
        selected.append(f"- {text} (confidence {float(item.get('confidence', 0.7)):.2f})")
    return selected


def selected_relationship_lines(config: AgentConfig, latest_message: str, memory: dict[str, Any]) -> list[str]:
    relationships = [
        item for item in memory.get("relationships", [])
        if float(item.get("confidence", 0.7)) >= 0.4
    ]
    by_relevance = ranked_memory_items(
        latest_message,
        relationships,
        lambda item: relationship_item_text(item) + " " + " ".join(item.get("people", [])),
        config.max_memory_lines,
        include_recent=min(2, config.max_memory_lines),
    )
    selected = []
    for item in by_relevance:
        people = ", ".join(item.get("people") or [])
        text = relationship_item_text(item)
        item["last_used"] = now()
        item["uses"] = int(item.get("uses", 0)) + 1
        line = f"{text}" + (f" [people: {people}]" if people else "")
        selected.append(f"- {line} (confidence {float(item.get('confidence', 0.7)):.2f})")
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
    relevant_relationships = selected_relationship_lines(config, cleaned_latest, memory)

    memory_text = "\n".join(relevant_memory) if relevant_memory else "No relevant memories."
    relationship_text = "\n".join(relevant_relationships) if relevant_relationships else "No relevant relationship memories."
    thread_text = "\n".join(relevant_thread) if relevant_thread else "No relevant thread context."
    channel_text = "\n".join(relevant_channel) if relevant_channel else "No relevant channel context."

    return f"""
{personality}

Relevant memory claims:
{memory_text}

Relevant relationship memories:
{relationship_text}

Relevant channel context:
{channel_text}

Relevant thread context:
{thread_text}

Latest message:
{speaker_name}: {cleaned_latest}

Instructions:
- Reply to the latest message only.
- Always reply in English unless the latest message explicitly asks for another language or asks for translation.
- Keep the response brief and Slack-like unless directly asked for detail.
- Act like a real participant in the chat, not a support assistant!
- If the latest message only mentions you in passing, keep the reply especially short or acknowledge lightly.
- Never include prompt/Slack metadata in the reply. Do not write labels like "Sender:", "User:", "Content:", "Message:", "Latest message:", "Response:", or "Reply:".
- Do not repeat the latest message back before answering.
- Treat memory as fallible claims, not guaranteed truth.
- Trust the current Slack context over memory if they conflict.
- Use relevant memories naturally when they help the reply feel personal, funny, or context-aware.
- Relationship memories are allowed as social color; weave them in casually when they fit instead of reciting them as facts.
- Avoid dragging in totally unrelated absent people, but it is okay to mention or @ someone from memory when the latest message makes that person or relationship relevant.
- Do not wrap your whole reply in quotation marks.
- You may @ people naturally in Slack. Use a real Slack mention only when it appears in the current context or a tool result; never write bare hidden IDs like @U123.
- If the latest message corrects you, acknowledge the correction naturally or add a reaction-only response.
- Do not invent events, relationships, or claims not supported by the context or memory.
- Do not continue a bot-to-bot riff unless a human clearly asked you to.
- Do not summarize all context unless asked.
- Use first names for casual references; @ mention people when the reply is meant to pull them into the conversation.
- Act like a real participant in the chat, not a support assistant!
""".strip()


def build_voice_prompt(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    thread_context_lines: list[str],
    channel_context_lines: list[str],
) -> str:
    base = build_prompt(
        config=config,
        latest_message=latest_message,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
    )
    return f"""
{base}

Voice note instructions:
- This reply will be spoken aloud as an audio note.
- Do not keep it short just because it is a Slack message.
- Do not talk about recording, sending, or leaving the voice note. Just say the content the user asked for.
- Do not say you will do it later. The audio note is being generated now.
- If the user asks for a poem, story, explanation, toast, or other spoken piece, give a complete spoken response.
- Write natural speech with clear sentences and light punctuation.
- Avoid emoji, markdown, bullets, links, and stage directions because they sound bad in TTS.
- Keep it under about 90 seconds unless the user explicitly asks for something longer.
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
    )
    return f"""
{base}

Tool used: {tool_name}
Tool result:
{json.dumps(tool_result, ensure_ascii=False, indent=2)}

Write the final Slack reply using the tool result as the source of truth.
- Always reply in English unless the latest message explicitly asks for another language or asks for translation.
- Do not contradict the tool result.
- If the tool result has matches, threads, lines, or profile fields, use those concrete details.
- If the tool result is empty, say that directly and briefly.
- Do not pretend you checked something beyond the tool result.
- Do not mention JSON or tools unless the user asked.
- Never include prompt/Slack metadata in the reply. Do not write labels like "Sender:", "User:", "Content:", "Message:", "Latest message:", "Response:", or "Reply:".
- Do not repeat the latest message back before answering.
- {"Keep it especially short because this was not an explicit request." if not explicit else "Answer the user's request directly."}
""".strip()


def build_multi_tool_result_prompt(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    memory: dict[str, Any],
    tool_runs: list[dict[str, Any]],
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
    )
    compact_runs = [
        {
            "tool": run.get("tool"),
            "arguments": run.get("arguments", {}),
            "result": run.get("result", {}),
        }
        for run in tool_runs
        if run.get("tool") not in {"react_to_message", "add_reaction_set"}
    ]
    return f"""
{base}

Tool results gathered so far:
{json.dumps(compact_runs, ensure_ascii=False, indent=2)[:12000]}

Write the final Slack reply using the tool results as the source of truth.
- Always reply in English unless the latest message explicitly asks for another language or asks for translation.
- Synthesize across all tool results; do not just dump them.
- Do not contradict tool results or invent facts beyond them.
- If the tools conflict, prefer the most recent/current result and say what was unclear.
- If a tool result is empty or inconclusive, say that briefly only if it matters to the answer.
- Do not mention JSON or internal tools unless the user asked.
- Never include prompt/Slack metadata in the reply. Do not write labels like "Sender:", "User:", "Content:", "Message:", "Latest message:", "Response:", or "Reply:".
- Do not repeat the latest message back before answering.
- For web results, sound like a friend who googled it. Do not refer to "the links", "the search results", "snippets", or "sources" as objects in the reply.
- If web results are inconclusive, say "I couldn't pin down..." or "I didn't find a clean answer..." instead of narrating what the links did not say.
- Do not include URLs unless the user specifically asked for links, sources, citations, or where you found it.
- If the query involves a weird acronym, nickname, typo-looking phrase, or possible inside joke, do not force a serious expansion from weak web results. Say it may be internal/context-specific if that is what the evidence suggests.
- {"Keep it especially short because this was not an explicit request." if not explicit else "Answer the user's request directly, with enough detail to be useful."}
""".strip()


def build_web_result_prompt(
    config: AgentConfig,
    latest_message: str,
    speaker_name: str,
    tool_result: dict[str, Any],
) -> str:
    personality = config.personality_path.read_text(encoding="utf-8").strip()
    results = tool_result.get("results") or []
    compact_results = [
        {
            "title": str(result.get("title") or "")[:180],
            "url": str(result.get("url") or "")[:300],
            "snippet": str(result.get("snippet") or "")[:700],
        }
        for result in results[:5]
    ]
    return f"""
{personality}

You are answering a Slack message using web search results.

Latest message:
{speaker_name}: {clean_slack_text(latest_message)}

Web search query:
{tool_result.get("query") or clean_slack_text(latest_message)}

Web search results:
{json.dumps(compact_results, ensure_ascii=False, indent=2)}

Instructions:
- Reply in English.
- Use ONLY the web search results above for factual claims.
- Do not use your own background knowledge to fill in missing names, dates, titles, or details.
- If the results do not clearly verify a detail, say it like a person: "I couldn't pin that down" or "I didn't find a clean answer." Do not say "the links/search results don't verify..."
- It is okay to make a clearly marked joke or opinion after the sourced answer, but do not invent factual support for it.
- Write like a friend who just googled it for them, not like a report or search-results page.
- Keep the useful organization, but avoid stiff numbered lists unless the user asked for a list.
- Prefer quick, natural phrasing like "I found a few things" or "Looks like..." when that fits.
- Do not refer to "the links", "the search results", "snippets", or "sources" as objects in the reply.
- Never include prompt/Slack metadata in the reply. Do not write labels like "Sender:", "User:", "Content:", "Message:", "Latest message:", "Response:", or "Reply:".
- Do not repeat the latest message back before answering.
- Do not include URLs unless the user specifically asked for links, sources, citations, or where you found it.
- If a phrase looks like a weird acronym, typo, nickname, or possible inside joke, do not assume a serious typo and do not confidently expand it from unrelated results.
- For acronyms, only give an expansion when the results clearly tie that exact acronym to the exact organization/context in the user's question.
- If the exact acronym/context is not clear, say it may be internal/context-specific or an inside joke, and keep the tone relaxed.
- Keep it Slack-like, but give enough detail to answer the question.
- Use first names instead of full names unless needed for clarity.
- Do not mention JSON or tools.
""".strip()


def direct_tool_reply(tool_name: str, tool_result: dict[str, Any], explicit: bool) -> str | None:
    if tool_name == "get_user_profile":
        if not tool_result.get("found"):
            return "I couldn't find that person in Slack."
        if tool_result.get("matches"):
            names = [
                f"{match.get('name') or match.get('real_name')} ({match.get('title')})" if match.get("title") else str(match.get("name") or match.get("real_name"))
                for match in tool_result.get("matches", [])[:3]
            ]
            return "I found " + ", ".join(names) + "."
        name = tool_result.get("real_name") or tool_result.get("name") or "That person"
        details = []
        if tool_result.get("title"):
            details.append(str(tool_result["title"]))
        if tool_result.get("tz"):
            details.append(f"timezone: {tool_result['tz']}")
        if tool_result.get("is_bot"):
            details.append("bot account")
        if details:
            return f"That's {name} - " + "; ".join(details) + "."
        return f"That's {name}."

    if tool_name == "list_recent_threads":
        threads = tool_result.get("threads") or []
        if not threads:
            return "I don't see any active threads recently."
        clean_lines = [
            f"- {thread.get('speaker')} started \"{thread.get('text')}\" around {thread.get('time')} ({thread.get('reply_count')} replies)"
            for thread in threads[:5]
        ]
        return "The active threads I see are:\n" + "\n".join(clean_lines)

    if tool_name == "search_channel_history":
        matches = tool_result.get("matches") or []
        query = tool_result.get("query") or "that"
        if not matches:
            return f"I didn't find recent channel mentions of {query}."
        if len(matches) == 1:
            return f"The most recent mention I found was: {matches[0]}"
        lines = [f"- {match}" for match in matches[:4]]
        return "Yep, I found these recent mentions:\n" + "\n".join(lines)

    if tool_name == "quote_recent_message":
        matches = tool_result.get("matches") or []
        if not matches:
            return "I couldn't find a recent matching message."
        if len(matches) == 1:
            msg = matches[0]
            return f"{msg.get('speaker')} said around {msg.get('time')}: \"{msg.get('text')}\""
        lines = [
            f"- {msg.get('speaker')} around {msg.get('time')}: \"{msg.get('text')}\""
            for msg in matches[:3]
        ]
        return "Closest recent messages I found:\n" + "\n".join(lines)

    if tool_name == "get_message_permalink":
        if tool_result.get("permalink"):
            return f"Here’s the message link: {tool_result['permalink']}"
        return "I couldn't get a link for that message."

    if tool_name == "inspect_reactions":
        reactions = tool_result.get("reactions") or []
        if not reactions:
            return "I don't see any reactions on that message."
        parts = [
            f":{reaction.get('name')}: x{reaction.get('count')}"
            for reaction in reactions[:8]
        ]
        return "Reactions I see: " + ", ".join(parts)

    if tool_name == "get_channel_context":
        lines = tool_result.get("lines") or []
        if not lines:
            return "I don't see much recent channel context."
        summary = str(tool_result.get("summary") or "").strip()
        if summary:
            return summary
        selected = lines[-8:]
        return "You mostly missed this:\n" + "\n".join(f"- {line}" for line in selected)

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


TOOL_ALLOWED_INTENTS = {
    "get_time": {"time_date", "elapsed_time"},
    "search_memory": {"memory_lookup"},
    "update_memory": {"memory_update"},
    "search_relationship_memory": {"memory_lookup", "relationship_memory_lookup"},
    "update_relationship_memory": {"memory_update", "relationship_memory_update"},
    "summarize_thread": {"thread_summary"},
    "get_channel_context": {"channel_catchup"},
    "get_user_profile": {"user_profile"},
    "list_recent_threads": {"active_threads"},
    "save_thread_summary": {"thread_summary"},
    "search_channel_history": {"channel_search"},
    "quote_recent_message": {"message_quote"},
    "get_message_permalink": {"message_permalink"},
    "inspect_reactions": {"reaction_inspection"},
    "set_reminder_note": {"reminder"},
    "react_to_message": {"reaction"},
    "add_reaction_set": {"reaction"},
    "read_link_preview": {"link_preview"},
    "search_web": {"web_search"},
    "send_voice_note": {"voice_note"},
}


def validate_router_tool_call(ctx: ToolContext, parsed: dict[str, Any]) -> tuple[bool, str]:
    tool_name = str(parsed.get("tool") or "")
    intent = str(parsed.get("intent") or "")
    try:
        confidence = float(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if tool_name not in ctx.config.enabled_tools:
        return False, "not_enabled"
    if confidence < 0.75:
        return False, "low_confidence"
    allowed = TOOL_ALLOWED_INTENTS.get(tool_name, set())
    if intent not in allowed:
        return False, f"intent_mismatch:{intent}"
    arguments = parsed.get("arguments") if isinstance(parsed.get("arguments"), dict) else {}
    if tool_name == "get_time" and not text_needs_time_tool(ctx.latest_message):
        return False, "time_not_requested"
    if tool_name == "read_link_preview" and not (arguments.get("url") or extract_urls(ctx.latest_message)):
        return False, "link_preview_no_url"
    if tool_name == "search_web" and not text_needs_web_search(ctx.latest_message):
        return False, "web_search_not_requested"
    if tool_name == "get_user_profile":
        if not arguments.get("lookup_requested"):
            return False, "profile_lookup_not_requested"
        if not text_has_slack_profile_hint(ctx.latest_message):
            return False, "profile_lookup_missing_slack_hint"
    if tool_name in {"react_to_message", "add_reaction_set"} and not ctx.explicit:
        if not deterministic_chance(
            f"{ctx.channel}:{ctx.latest_ts}:{arguments.get('emoji', '')}:{arguments.get('emojis', '')}",
            ctx.config.reaction_response_chance,
        ):
            return False, "reaction_sampled_out"
    return True, "ok"


def build_tool_router_prompt(ctx: ToolContext) -> str:
    thread_text = "\n".join(ctx.thread_context_lines[-ctx.config.max_thread_lines:]) or "No thread context."
    channel_text = "\n".join(ctx.channel_context_lines[-ctx.config.max_channel_lines:]) or "No channel context."
    tool_history = [
        {
            "tool": run.get("tool"),
            "arguments": run.get("arguments", {}),
            "result": run.get("result", {}),
        }
        for run in ctx.tool_history
    ]
    tool_history_text = json.dumps(tool_history, ensure_ascii=False, indent=2)[:9000] if tool_history else "No tools used yet."
    return f"""
You are a routing model for a Slack chat agent. Decide if the latest message needs a tool.

Available tools:
{enabled_tool_descriptions(ctx.config)}

Classify the user's intent first. Use tools based on semantic intent, not exact wording.
Valid intents: casual_chat, correction, time_date, elapsed_time, memory_lookup, memory_update, relationship_memory_lookup, relationship_memory_update, thread_summary, channel_catchup, user_profile, active_threads, channel_search, message_quote, message_permalink, reaction_inspection, reminder, reaction, link_preview, web_search, voice_note, none.

Tool policy:
- get_time only for time_date or elapsed_time when the user is actually asking for a date, clock time, timezone, when something happened, or elapsed duration. Do not use get_time for casual words like now, rn, tonight, soon, later, sleep, nap, or "don't bother me rn".
- search_memory only for memory_lookup.
- update_memory only for memory_update.
- search_relationship_memory for relationship_memory_lookup, or memory_lookup when the user asks about interpersonal context, friendships, rivalries, who likes/dislikes whom, inside jokes between people, or social history.
- update_relationship_memory for relationship_memory_update, or memory_update when the user asks you to remember interpersonal context, friendships, rivalries, who likes/dislikes whom, inside jokes between people, or social history.
- summarize_thread only for thread_summary.
- get_channel_context only for channel_catchup.
- get_user_profile only for user_profile, and only when the user is asking about a Slack/workspace user, Slack profile, title, role, timezone, account, or uses an actual Slack user mention/id. Do not use it for celebrities, creators, athletes, public figures, or internet names. Set arguments.lookup_requested=true when using it.
- list_recent_threads only for active_threads.
- search_channel_history only for channel_search.
- quote_recent_message only for message_quote when the user asks what someone said, asks to quote a recent message, asks who mentioned a topic, or asks for the exact wording of a recent Slack message.
- get_message_permalink only for message_permalink when the user asks for a link/permalink to a message or thread.
- inspect_reactions only for reaction_inspection when the user asks who reacted, what reactions a message has, or how people reacted.
- set_reminder_note only for reminder.
- react_to_message only for reaction. Use reactions generously for lightweight participation: jokes, wins, agreement, chaos, sympathy, shade, curiosity, or "I saw this" energy.
- add_reaction_set only for reaction when 2-3 emoji reactions would feel more natural than one, such as big wins, funny chaos, dramatic updates, or strong agreement. Use at most 3 common Slack emoji names.
- Vary reaction choices. Prefer context-specific emoji over thumbsup. Useful options include: joy, skull, sob, eyes, thinking_face, face_with_raised_eyebrow, clap, tada, fire, raised_hands, rocket, partying_face, heart, blue_heart, pray, saluting_face, melting_face, grimacing, sweat_smile, facepalm, 100, ok_hand.
- Use thumbsup sparingly, mostly for direct requests or simple acknowledgement.
- read_link_preview only for link_preview when the latest message includes a URL and the user asks about the link, asks for thoughts on it, or a preview would materially improve the reply.
- search_web only for web_search when the user asks you to search/look up/check current public web information, asks a public/general-knowledge factual question that memory and Slack tools cannot answer, asks who a public figure/creator/internet personality is, or asks about latest/current public facts that are not in Slack context. Do not search the web for casual chat, jokes, opinions, private Slack context, memory lookup, reminders, channel catch-up, or thread summaries.
- send_voice_note ONLY for voice_note, when an audio or voice response is requested. Do not provide the spoken text; the main model will write the reply.
- Use none for casual_chat and correction. Those should be answered conversationally by the main model.
- If tool results so far are enough to answer well, choose none.
- If another tool would materially improve the answer, choose that next tool. Do not repeat a tool call with the same arguments.

Recent channel context:
{channel_text}

Thread context:
{thread_text}

Latest message:
{ctx.speaker_name}: {clean_slack_text(ctx.latest_message)}

Tool results so far:
{tool_history_text}

Return only JSON. Include confidence from 0 to 1. Use one of:
{{"type":"none","intent":"casual_chat","confidence":0.9}}
{{"type":"tool_call","intent":"time_date","confidence":0.9,"tool":"get_time","arguments":{{"timezone":"America/New_York"}}}}
{{"type":"tool_call","intent":"memory_lookup","confidence":0.9,"tool":"search_memory","arguments":{{"query":"topic","limit":5}}}}
{{"type":"tool_call","intent":"memory_update","confidence":0.9,"tool":"update_memory","arguments":{{"text":"fact","confidence":0.7}}}}
{{"type":"tool_call","intent":"relationship_memory_lookup","confidence":0.9,"tool":"search_relationship_memory","arguments":{{"query":"people or relationship","limit":5}}}}
{{"type":"tool_call","intent":"relationship_memory_update","confidence":0.9,"tool":"update_relationship_memory","arguments":{{"text":"relationship fact","people":["name"],"confidence":0.7}}}}
{{"type":"tool_call","intent":"thread_summary","confidence":0.9,"tool":"summarize_thread","arguments":{{"focus":"what to summarize"}}}}
{{"type":"tool_call","intent":"channel_catchup","confidence":0.9,"tool":"get_channel_context","arguments":{{"limit":10}}}}
{{"type":"tool_call","intent":"user_profile","confidence":0.9,"tool":"get_user_profile","arguments":{{"user":"name or user id","lookup_requested":true}}}}
{{"type":"tool_call","intent":"active_threads","confidence":0.9,"tool":"list_recent_threads","arguments":{{"limit":8}}}}
{{"type":"tool_call","intent":"thread_summary","confidence":0.9,"tool":"save_thread_summary","arguments":{{"summary":"decision or outcome"}}}}
{{"type":"tool_call","intent":"channel_search","confidence":0.9,"tool":"search_channel_history","arguments":{{"query":"topic","limit":8}}}}
{{"type":"tool_call","intent":"message_quote","confidence":0.9,"tool":"quote_recent_message","arguments":{{"query":"topic or phrase","speaker":"optional person"}}}}
{{"type":"tool_call","intent":"message_permalink","confidence":0.9,"tool":"get_message_permalink","arguments":{{"ts":"optional message timestamp"}}}}
{{"type":"tool_call","intent":"reaction_inspection","confidence":0.9,"tool":"inspect_reactions","arguments":{{"ts":"optional message timestamp"}}}}
{{"type":"tool_call","intent":"reminder","confidence":0.9,"tool":"set_reminder_note","arguments":{{"text":"thing to remember","due":"when"}}}}
{{"type":"tool_call","intent":"reaction","confidence":0.9,"tool":"react_to_message","arguments":{{"emoji":"eyes"}}}}
{{"type":"tool_call","intent":"reaction","confidence":0.9,"tool":"add_reaction_set","arguments":{{"emojis":["tada","fire","rocket"]}}}}
{{"type":"tool_call","intent":"link_preview","confidence":0.9,"tool":"read_link_preview","arguments":{{"url":"https://example.com/article"}}}}
{{"type":"tool_call","intent":"web_search","confidence":0.9,"tool":"search_web","arguments":{{"query":"latest public information to search"}}}}
{{"type":"tool_call","intent":"voice_note","confidence":0.9,"tool":"send_voice_note","arguments":{{}}}}

Do not include explanations. Do not include multiple JSON objects.
If confidence is below 0.75, choose none. Prefer none for casual greetings, status check-ins, corrections, opinions, or when no listed tool would materially improve the answer.
""".strip()


def route_tool_call(ctx: ToolContext) -> dict[str, Any] | None:
    if not ctx.config.enabled_tools:
        event_log(ctx.config, "tool_router", decision="none", reason="no_enabled_tools")
        return None
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
    valid, reason = validate_router_tool_call(ctx, parsed)
    if not valid:
        if reason == "profile_lookup_missing_slack_hint" and "search_web" in ctx.config.enabled_tools and text_needs_web_search(ctx.latest_message):
            fallback = {
                "type": "tool_call",
                "intent": "web_search",
                "confidence": parsed.get("confidence", 0.8),
                "tool": "search_web",
                "arguments": {"query": clean_slack_text(ctx.latest_message)},
            }
            event_log(ctx.config, "tool_router", decision="fallback", reason=reason, from_tool=tool_name, tool="search_web", parsed=fallback)
            return fallback
        event_log(ctx.config, "tool_router", decision="rejected", reason=reason, raw=raw[:1000], parsed=parsed)
        return None
    event_log(ctx.config, "tool_router", decision="tool_call", route="model", tool=tool_name, parsed=parsed)
    return parsed


def tool_call_signature(tool_call: dict[str, Any]) -> str:
    return json.dumps(
        {
            "tool": tool_call.get("tool"),
            "arguments": tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {},
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def context_with_tool_history(ctx: ToolContext, tool_runs: list[dict[str, Any]]) -> ToolContext:
    return ToolContext(
        config=ctx.config,
        client=ctx.client,
        memory=ctx.memory,
        channel=ctx.channel,
        thread_ts=ctx.thread_ts,
        latest_ts=ctx.latest_ts,
        latest_message=ctx.latest_message,
        speaker_name=ctx.speaker_name,
        thread_context_lines=ctx.thread_context_lines,
        channel_context_lines=ctx.channel_context_lines,
        explicit=ctx.explicit,
        tool_history=tuple(tool_runs),
    )


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
    allow_conversational_reply: bool,
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
        explicit=explicit,
    )
    tool_runs: list[dict[str, Any]] = []
    seen_tool_calls: set[str] = set()
    deadline = time.monotonic() + MAX_TOOL_RUNTIME_SECONDS

    for _ in range(MAX_ITERATIVE_TOOL_CALLS):
        if time.monotonic() >= deadline:
            event_log(config, "tool_loop_stopped", reason="deadline", runs=len(tool_runs))
            break
        loop_context = context_with_tool_history(tool_context, tool_runs)
        routed_tool_call = route_tool_call(loop_context)
        if not routed_tool_call:
            break
        signature = tool_call_signature(routed_tool_call)
        if signature in seen_tool_calls:
            event_log(config, "tool_loop_stopped", reason="duplicate_tool_call", signature=signature)
            break
        seen_tool_calls.add(signature)
        tool_run = run_tool_call(routed_tool_call, loop_context)
        if not tool_run:
            break
        tool_name, tool_result = tool_run
        arguments = routed_tool_call.get("arguments") if isinstance(routed_tool_call.get("arguments"), dict) else {}
        tool_runs.append({"tool": tool_name, "arguments": arguments, "result": tool_result})
        if tool_name == "send_voice_note":
            save_memory(config, memory)
            return str(tool_result.get("reply_text") or "").strip()
        if tool_name in {"react_to_message", "add_reaction_set"} and not explicit:
            save_memory(config, memory)
            return str(tool_result.get("reply_text") or "").strip()

    if tool_runs:
        final_context = context_with_tool_history(tool_context, tool_runs)
        answer_runs = [run for run in tool_runs if run.get("tool") not in {"react_to_message", "add_reaction_set"}]
        if not answer_runs:
            if explicit:
                reply = call_ollama(
                    config,
                    build_prompt(
                        config=config,
                        latest_message=latest_message,
                        speaker_name=speaker_name,
                        memory=memory,
                        thread_context_lines=thread_context_lines,
                        channel_context_lines=channel_context_lines,
                    ),
                )
                reply = sanitize_reply_text(config, reply, latest_message, client)
                save_memory(config, memory)
                return maybe_send_voice_instead(final_context, reply)
            save_memory(config, memory)
            return ""
        if len(answer_runs) == 1:
            only = answer_runs[0]
            direct_reply = direct_tool_reply(str(only.get("tool") or ""), only.get("result") or {}, explicit)
            if direct_reply:
                save_memory(config, memory)
                return maybe_send_voice_instead(final_context, sanitize_reply_text(config, direct_reply, latest_message, client))
        reply = call_ollama(
            config,
            build_multi_tool_result_prompt(
                config=config,
                latest_message=latest_message,
                speaker_name=speaker_name,
                memory=memory,
                tool_runs=tool_runs,
                thread_context_lines=thread_context_lines,
                channel_context_lines=channel_context_lines,
                explicit=explicit,
            ),
            num_predict=220,
            temperature=0.2,
        )
        reply = sanitize_reply_text(config, reply, latest_message, client)
        save_memory(config, memory)
        return maybe_send_voice_instead(final_context, reply)

    if not explicit and not allow_conversational_reply:
        if (
            config.ambient_reaction_fallback_chance > 0
            and "react_to_message" in config.enabled_tools
            and deterministic_chance(f"ambient-reaction:{channel}:{latest_ts}:{latest_message}", config.ambient_reaction_fallback_chance)
        ):
            emoji = contextual_reaction_emoji(latest_message, latest_ts)
            result = tool_react_to_message({"emoji": emoji}, tool_context)
            event_log(config, "ambient_reaction_fallback", emoji=emoji, result=result)
        save_memory(config, memory)
        return ""

    prompt = build_prompt(
        config=config,
        latest_message=latest_message,
        speaker_name=speaker_name,
        memory=memory,
        thread_context_lines=thread_context_lines,
        channel_context_lines=channel_context_lines,
    )
    reply = call_ollama(config, prompt)
    reply = sanitize_reply_text(config, reply, latest_message, client)
    save_memory(config, memory)
    return maybe_send_voice_instead(tool_context, reply)


def should_respond_to_channel_message(
    config: AgentConfig,
    text: str,
    ambient_allowed: bool = True,
    is_bot_message: bool = False,
    ambient_channel_chance: float | None = None,
    chance_key: str | None = None,
) -> ResponseDecision:
    if text_directly_addresses_agent(config, text) or text_asks_agent_question(config, text):
        return ResponseDecision(True, True, "direct")
    if text_is_passive_agent_mention(config, text):
        return ResponseDecision(False, False, "passive_name_mention")
    if is_bot_message:
        return ResponseDecision(False, False, "bot_ambient_suppressed")
    if not ambient_allowed:
        return ResponseDecision(False, False, "explicit_only_channel")
    if ambient_channel_chance is not None and not deterministic_chance(chance_key or text, ambient_channel_chance):
        return ResponseDecision(False, False, "channel_ambient_sampled_out")
    if text_is_correction_or_disagreement(text):
        return ResponseDecision(True, False, "ambient_correction")
    if text_invites_room_response(text) and deterministic_chance(text, config.ambient_response_chance):
        return ResponseDecision(True, False, "ambient_room_prompt")
    return ResponseDecision(True, False, "ambient_router_candidate")


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
    if text_is_passive_agent_mention(config, text):
        return ResponseDecision(False, False, "passive_name_mention")
    if not ambient_allowed:
        return ResponseDecision(False, False, "explicit_only_channel")
    if text_is_correction_or_disagreement(text):
        return ResponseDecision(True, False, "active_thread_correction")
    if text_asks_active_followup(text):
        return ResponseDecision(True, False, "active_followup")
    if text_is_short_thread_followup(text):
        return ResponseDecision(True, False, "active_thread_short_followup")
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
    reply_in_thread: bool,
    allow_conversational_reply: bool,
) -> None:
    start = time.time()
    text = event.get("text", "")
    speaker_name = get_event_speaker_name(config, client, event)

    command_reply = handle_memory_commands(config, text, memory, speaker_name)
    if command_reply:
        if reply_in_thread:
            say(text=command_reply, thread_ts=thread_ts)
        else:
            say(text=command_reply)
        record_bot_reply(config, memory, channel, thread_ts, explicit=True)
        event_log(config, "memory_command_reply", channel=channel, thread_ts=thread_ts, text=text, reply=command_reply)
        debug(config, f"Memory command replied in {time.time() - start:.2f}s")
        return

    auto_capture_memory(config, text, memory, speaker_name)

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
        allow_conversational_reply=allow_conversational_reply,
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

    if reply_in_thread:
        say(text=reply, thread_ts=thread_ts)
    else:
        say(text=reply)
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
            reply_in_thread="thread_ts" in event,
            allow_conversational_reply=True,
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
        ambient_channel_chance = configured_channel_chance(config, client, channel)

        if not is_thread_reply:
            decision = should_respond_to_channel_message(
                config,
                text,
                ambient_allowed=ambient_allowed,
                is_bot_message=is_bot_message,
                ambient_channel_chance=ambient_channel_chance,
                chance_key=f"{channel}:{event.get('ts')}:{text}",
            )
            event_log(
                config,
                "response_decision",
                surface="channel",
                channel=channel,
                ts=event.get("ts"),
                text=text,
                ambient_allowed=ambient_allowed,
                ambient_channel_chance=ambient_channel_chance,
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
                reply_in_thread=False,
                allow_conversational_reply=decision.reason != "ambient_router_candidate",
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
            reply_in_thread=True,
            allow_conversational_reply=decision.reason != "ambient_router_candidate",
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
