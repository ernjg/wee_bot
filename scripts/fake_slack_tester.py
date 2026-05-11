import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot


class FakeResponse:
    def __init__(self, text: str, url: str = "https://example.com", status_code: int = 200, content_type: str = "text/html") -> None:
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = {"content-type": content_type}


@dataclass
class Expected:
    decision: str | None = None
    should_respond: bool | None = None
    message_count: int | None = None
    reaction_count: int | None = None
    file_count: int | None = None
    reply_in_thread: bool | None = None
    text_contains: str | None = None
    reaction: str | None = None
    memory_contains: str | None = None
    relationship_contains: str | None = None
    no_initial_comment: bool = False
    uploaded_text_contains: str | None = None


@dataclass
class Case:
    name: str
    text: str
    surface: str = "channel"
    channel: str = "CFAKE"
    thread_ts: str | None = None
    thread_active: bool = False
    is_bot_message: bool = False
    config_overrides: dict[str, Any] = field(default_factory=dict)
    expected: Expected = field(default_factory=Expected)


class FakeSay:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def __call__(self, *, text: str, thread_ts: str | None = None) -> None:
        self.messages.append({"text": text, "thread_ts": thread_ts})


class FakeClient:
    def __init__(self) -> None:
        self.reactions: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.history = [
            {"ts": "1778359000.000001", "user": "U024R1SFT5J", "text": "we shipped the reaction fix", "reply_count": 3},
            {"ts": "1778359060.000001", "user": "U024R1SFT5J", "text": "wee, what did I miss?"},
            {"ts": "1778359120.000001", "user": "U057QT233PT", "text": "slack scopes came up earlier"},
            {"ts": "1778359180.000001", "user": "U024R1SFT5J", "text": "we decided to test channel replies too"},
        ]

    def users_info(self, user: str) -> dict:
        return {
            "user": {
                "id": user,
                "name": "ajpatt27",
                "tz": "America/New_York",
                "is_bot": False,
                "profile": {
                    "display_name": "ajpatt27",
                    "real_name": "Andre Patterson",
                    "title": "TKD Co-Social Chair",
                },
            }
        }

    def users_list(self, limit: int = 200) -> dict:
        return {"members": [self.users_info("U057QU46B25")["user"]]}

    def conversations_info(self, channel: str) -> dict:
        names = {"CFAKE": "general", "CANNOUNCE": "announce"}
        return {"channel": {"id": channel, "name": names.get(channel, channel)}}

    def conversations_history(self, **kwargs) -> dict:
        return {"messages": list(reversed(self.history))}

    def conversations_replies(self, **kwargs) -> dict:
        return {"messages": self.history}

    def reactions_get(self, **kwargs) -> dict:
        if kwargs.get("full"):
            return {
                "message": {
                    "text": "we shipped the reaction fix",
                    "reactions": [
                        {"name": "tada", "count": 2, "users": ["U024R1SFT5J", "U057QT233PT"]},
                        {"name": "fire", "count": 1, "users": ["U057QT233PT"]},
                    ],
                }
            }
        return {"message": {"reactions": []}}

    def reactions_add(self, **kwargs) -> None:
        self.reactions.append(kwargs)

    def chat_getPermalink(self, **kwargs) -> dict:
        return {"ok": True, "permalink": f"https://fake.slack.com/archives/{kwargs.get('channel')}/p{kwargs.get('message_ts', '').replace('.', '')}"}

    def files_upload_v2(self, **kwargs) -> dict:
        self.files.append(kwargs)
        return {"ok": True, "file": {"id": f"F{len(self.files):04d}", "title": kwargs.get("title")}}


def fake_ollama(config, prompt, model=None, num_predict=None, temperature=None):
    if "You are answering a Slack message using web search results." in prompt:
        if "Sigma Nu" in prompt or "sigma nu" in prompt.lower():
            return "The search result I found says Sigma Nu was founded at VMI. I can't verify the founder names from these snippets, but yeah, I’d still bet the founders would roast anyone showing up late."
        return "tool-backed reply"
    if "Automatic memory extraction for a Slack chat agent." in prompt:
        if "Andre's favorite color is purple" in prompt:
            return json.dumps({"memories": [{"text": "Andre's favorite color is purple.", "confidence": 0.8}], "relationships": []})
        if "Ernesto and Andre are poker rivals" in prompt:
            return json.dumps({"memories": [], "relationships": [{"text": "Ernesto and Andre are poker rivals.", "people": ["Ernesto", "Andre"], "confidence": 0.8}]})
        if "brios is flakey" in prompt.lower():
            return json.dumps({"memories": [], "relationships": [{"text": "Brios is flakey.", "people": ["Brios"], "confidence": 0.75}]})
        return json.dumps({"memories": [], "relationships": []})
    if "Tool results gathered so far:" in prompt:
        if "Sigma Nu" in prompt or "sigma nu" in prompt.lower():
            return "The search result I found says Sigma Nu was founded at VMI. I can't verify the founder names from these snippets, but yeah, Iâ€™d still bet the founders would roast anyone showing up late."
        return "tool-backed reply"

    if "You are a routing model" not in prompt and "Latest message:" in prompt and "quote regression" in prompt.lower():
        return '"normal conversational reply"'
    if "You are a routing model" not in prompt and "Latest message:" in prompt and "absent person regression" in prompt.lower():
        return "Irdnennam would probably have a take on that."
    if "You are a routing model" not in prompt and "Latest message:" in prompt and "slack id regression" in prompt.lower():
        return "@Ernesto Gomez Nah, I don't think poker is my game tonight. You should probably focus on stopping @U0B22FZF3UN from cleaning up all your chips."
    if "You are a routing model" not in prompt and "Latest message:" in prompt and "sender content regression" in prompt.lower():
        return "Sender: Ernesto\nContent: wee, sender content regression\nResponse: normal conversational reply"

    if "Summarize what happened recently" in prompt:
        return "Recently, people tested reactions, Slack scopes, and channel catch-up behavior."

    if "Summarize this Slack thread" in prompt:
        return "- The thread covered reaction testing\n- Slack scopes came up\n- Channel replies were discussed"

    if "You are a routing model" in prompt:
        latest_block = prompt.split("Latest message:", 1)[-1].strip()
        latest = latest_block.splitlines()[0].lower()
        if "low confidence time" in latest:
            return json.dumps({"type": "tool_call", "intent": "time_date", "confidence": 0.4, "tool": "get_time", "arguments": {}})
        if "wrong tool" in latest:
            return json.dumps({"type": "tool_call", "intent": "casual_chat", "confidence": 0.95, "tool": "get_time", "arguments": {}})
        if "dont bother me rn" in latest or "don't bother me rn" in latest:
            return json.dumps({"type": "tool_call", "intent": "time_date", "confidence": 0.92, "tool": "get_time", "arguments": {}})
        if "time" in latest or "how long ago" in latest:
            return json.dumps({"type": "tool_call", "intent": "time_date", "confidence": 0.92, "tool": "get_time", "arguments": {}})
        if "what did i miss" in latest or "catch me up" in latest:
            return json.dumps({"type": "tool_call", "intent": "channel_catchup", "confidence": 0.9, "tool": "get_channel_context", "arguments": {"limit": 25}})
        if "summarize this thread" in latest:
            return json.dumps({"type": "tool_call", "intent": "thread_summary", "confidence": 0.9, "tool": "summarize_thread", "arguments": {"focus": "latest request"}})
        if "profile misroute" in latest:
            return json.dumps({"type": "tool_call", "intent": "user_profile", "confidence": 0.9, "tool": "get_user_profile", "arguments": {"user": "Ernesto Gomez"}})
        if "search web who is ishowspeed" in latest:
            return json.dumps({"type": "tool_call", "intent": "web_search", "confidence": 0.9, "tool": "search_web", "arguments": {"query": "who is ishowspeed"}})
        if "who is ishowspeed" in latest:
            return json.dumps({"type": "tool_call", "intent": "user_profile", "confidence": 0.9, "tool": "get_user_profile", "arguments": {"user": "ishowspeed", "lookup_requested": True}})
        if "what is the capital of mongolia" in latest:
            return json.dumps({"type": "tool_call", "intent": "web_search", "confidence": 0.9, "tool": "search_web", "arguments": {"query": "what is the capital of Mongolia"}})
        if "who founded sigma nu" in latest:
            return json.dumps({"type": "tool_call", "intent": "web_search", "confidence": 0.9, "tool": "search_web", "arguments": {"query": "who founded Sigma Nu"}})
        if "who is" in latest:
            return json.dumps({"type": "tool_call", "intent": "user_profile", "confidence": 0.9, "tool": "get_user_profile", "arguments": {"user": "@U057QU46B25", "lookup_requested": True}})
        if "search memory" in latest:
            return json.dumps({"type": "tool_call", "intent": "memory_lookup", "confidence": 0.9, "tool": "search_memory", "arguments": {"query": "favorite color"}})
        if "search relationship" in latest:
            return json.dumps({"type": "tool_call", "intent": "relationship_memory_lookup", "confidence": 0.9, "tool": "search_relationship_memory", "arguments": {"query": "ernesto andre"}})
        if "remember relationship structured" in latest:
            return json.dumps({"type": "tool_call", "intent": "relationship_memory_update", "confidence": 0.9, "tool": "update_relationship_memory", "arguments": {"text": "Ernesto and Andre have a friendly rivalry about poker", "people": ["Ernesto", "Andre"], "confidence": 0.8}})
        if "remember structured" in latest:
            return json.dumps({"type": "tool_call", "intent": "memory_update", "confidence": 0.9, "tool": "update_memory", "arguments": {"text": "Andre's favorite color is green", "confidence": 0.8}})
        if "active threads" in latest:
            return json.dumps({"type": "tool_call", "intent": "active_threads", "confidence": 0.9, "tool": "list_recent_threads", "arguments": {"limit": 5}})
        if "talk about scopes" in latest:
            return json.dumps({"type": "tool_call", "intent": "channel_search", "confidence": 0.9, "tool": "search_channel_history", "arguments": {"query": "scopes", "limit": 5}})
        if "quote recent" in latest:
            return json.dumps({"type": "tool_call", "intent": "message_quote", "confidence": 0.9, "tool": "quote_recent_message", "arguments": {"query": "reaction fix", "limit": 10}})
        if "message link" in latest:
            return json.dumps({"type": "tool_call", "intent": "message_permalink", "confidence": 0.9, "tool": "get_message_permalink", "arguments": {}})
        if "inspect reactions" in latest:
            return json.dumps({"type": "tool_call", "intent": "reaction_inspection", "confidence": 0.9, "tool": "inspect_reactions", "arguments": {}})
        if "remind me" in latest:
            return json.dumps({"type": "tool_call", "intent": "reminder", "confidence": 0.9, "tool": "set_reminder_note", "arguments": {"text": "check the Slack bot logs", "due": "tomorrow"}})
        if "reaction set" in latest:
            return json.dumps({"type": "tool_call", "intent": "reaction", "confidence": 0.9, "tool": "add_reaction_set", "arguments": {"emojis": ["tada", "fire", "raised_hands"]}})
        if "link preview" in latest:
            return json.dumps({"type": "tool_call", "intent": "link_preview", "confidence": 0.9, "tool": "read_link_preview", "arguments": {}})
        if "search web" in latest:
            return json.dumps({"type": "tool_call", "intent": "web_search", "confidence": 0.9, "tool": "search_web", "arguments": {"query": "kokoro tts latest"}})
        if "web misroute" in latest:
            return json.dumps({"type": "tool_call", "intent": "web_search", "confidence": 0.9, "tool": "search_web", "arguments": {"query": "unneeded"}})
        if "link misroute" in latest:
            return json.dumps({"type": "tool_call", "intent": "link_preview", "confidence": 0.9, "tool": "read_link_preview", "arguments": {}})
        if "voice note" in latest or "say that out loud" in latest:
            return json.dumps({"type": "tool_call", "intent": "voice_note", "confidence": 0.9, "tool": "send_voice_note", "arguments": {"text": "This placeholder must be ignored."}})
        if "not your best friend" in latest:
            return json.dumps({"type": "tool_call", "intent": "reaction", "confidence": 0.9, "tool": "react_to_message", "arguments": {"emoji": "thumbsup"}})
        if "please react" in latest:
            return json.dumps({"type": "tool_call", "intent": "reaction", "confidence": 0.9, "tool": "react_to_message", "arguments": {"emoji": "thumbsup"}})
        if "shipped" in latest or "huge win" in latest or "thank you" in latest:
            return json.dumps({"type": "tool_call", "intent": "reaction", "confidence": 0.86, "tool": "react_to_message", "arguments": {"emoji": "tada"}})
        return json.dumps({"type": "none", "intent": "casual_chat", "confidence": 0.9})

    if "Tool used:" in prompt:
        return "tool-backed reply"
    return "normal conversational reply"


def fake_requests_get(url, **kwargs):
    if "duckduckgo.com" in str(url):
        return FakeResponse(
            """
            <html><body>
              <a class="result__a" href="https://example.com/kokoro">Kokoro TTS result</a>
              <a class="result__snippet">Current public info about Kokoro TTS.</a>
            </body></html>
            """,
            url="https://duckduckgo.com/html/",
        )
    return FakeResponse(
        """
        <html>
          <head>
            <title>Example Article</title>
            <meta name="description" content="A useful test article for link preview.">
          </head>
          <body><p>This is the article body excerpt.</p></body>
        </html>
        """,
        url=str(url),
    )


def make_config(memory_path: Path, **overrides: Any) -> bot.AgentConfig:
    values = {
        "name": "wee",
        "display_name": "Wee",
        "root": Path("agents/example"),
        "memory_path": memory_path,
        "personality_path": Path("agents/example/personality.txt"),
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "ollama_model": "fake",
        "tool_router_model": "fake",
        "bot_user_id": "U0B22FZF3UN",
        "triggers": ("wee", "wee marquez"),
        "about_phrases": ("wee", "you"),
        "pronoun_about_phrases": ("he", "him", "his"),
        "explicit_only_channels": (),
        "ambient_channel_chances": {},
        "enabled_tools": bot.DEFAULT_ENABLED_TOOLS,
        "debug": False,
        "event_log_enabled": False,
        "event_log_path": Path("logs/fake.events.jsonl"),
        "active_thread_ttl_seconds": 43200,
        "max_active_threads": 100,
        "thread_fetch_limit": 40,
        "channel_fetch_limit": 30,
        "max_thread_lines": 12,
        "max_channel_lines": 6,
        "max_memory_lines": 8,
        "always_keep_recent_thread": 5,
        "always_keep_recent_channel": 2,
        "ollama_timeout_seconds": 90,
        "ollama_num_predict": 120,
        "ollama_num_ctx": 4096,
        "ollama_temperature": 0.7,
        "max_auto_replies_per_thread": 6,
        "ambient_response_chance": 1.0,
        "thread_ambient_response_chance": 1.0,
        "reaction_response_chance": 1.0,
        "ambient_reaction_fallback_chance": 0.0,
        "voice_enabled": False,
        "voice_provider": "fake",
        "voice_model": "fake-tts",
        "voice_voices_path": "fake-voices",
        "voice_python_exe": sys.executable,
        "voice_name": "fake",
        "voice_language": "en-us",
        "voice_speed": 1.0,
        "voice_format": "mp3",
        "voice_max_chars": 600,
        "voice_response_chance": 0.0,
        "voice_disclosure": "",
        "max_memories": 200,
        "low_confidence_memory_ttl_days": 30,
    }
    values.update(overrides)
    return bot.AgentConfig(**values)


def decide(config: bot.AgentConfig, case: Case, memory: dict[str, Any]) -> bot.ResponseDecision:
    if case.surface == "thread":
        state = None
        if case.thread_active:
            thread_ts = case.thread_ts or "1778358000.000001"
            state = {"last_reply_at": bot.now(), "auto_reply_count": 0, "explicit": True}
            memory.setdefault("active_threads", {})[f"{case.channel}:{thread_ts}"] = state
        return bot.should_respond_to_thread_reply(
            config,
            case.text,
            state,
            case.is_bot_message,
            ambient_allowed=not bot.channel_is_explicit_only(config, FakeClient(), case.channel),
        )
    return bot.should_respond_to_channel_message(
        config,
        case.text,
        ambient_allowed=not bot.channel_is_explicit_only(config, FakeClient(), case.channel),
        is_bot_message=case.is_bot_message,
        ambient_channel_chance=bot.configured_channel_chance(config, FakeClient(), case.channel),
        chance_key=f"{case.channel}:fake-ts:{case.text}",
    )


def run_case(config: bot.AgentConfig, client: FakeClient, case: Case, index: int) -> dict[str, Any]:
    ts = f"1778359{index:03d}.000001"
    thread_ts = case.thread_ts or ("1778358000.000001" if case.surface == "thread" else ts)
    event = {
        "type": "message",
        "user": "U024R1SFT5J",
        "channel": case.channel,
        "ts": ts,
        "event_ts": ts,
        "text": case.text,
    }
    if case.surface == "thread":
        event["thread_ts"] = thread_ts
    if case.is_bot_message:
        event["bot_id"] = "BFAKE"

    memory = bot.load_memory(config)
    memory.setdefault("memories", []).append(bot.make_memory_item("Andre's favorite color is green", "tester", 0.8))
    memory.setdefault("relationships", []).append(bot.make_relationship_item("Ernesto and Andre have a friendly poker rivalry.", ["Ernesto", "Andre"], "tester", 0.8))
    decision = decide(config, case, memory)
    say = FakeSay()
    reaction_start = len(client.reactions)
    file_start = len(client.files)
    if decision.should_respond:
        bot.respond_as_agent(
            config=config,
            event=event,
            say=say,
            client=client,
            memory=memory,
            channel=case.channel,
            thread_ts=thread_ts,
            include_channel_context=case.surface == "channel",
            explicit=decision.explicit,
            reply_in_thread=case.surface == "thread",
            allow_conversational_reply=decision.reason != "ambient_router_candidate",
        )

    saved = bot.load_memory(config)
    return {
        "name": case.name,
        "text": case.text,
        "surface": case.surface,
        "decision": decision.reason,
        "should_respond": decision.should_respond,
        "messages": say.messages,
        "reactions": client.reactions[reaction_start:],
        "files": client.files[file_start:],
        "memory": saved,
    }


def check_result(case: Case, result: dict[str, Any]) -> list[str]:
    errors = []
    expected = case.expected
    messages = result["messages"]
    reactions = result["reactions"]
    files = result["files"]
    if expected.decision is not None and result["decision"] != expected.decision:
        errors.append(f"decision expected {expected.decision!r}, got {result['decision']!r}")
    if expected.should_respond is not None and result["should_respond"] != expected.should_respond:
        errors.append(f"should_respond expected {expected.should_respond}, got {result['should_respond']}")
    if expected.message_count is not None and len(messages) != expected.message_count:
        errors.append(f"message_count expected {expected.message_count}, got {len(messages)}")
    if expected.reaction_count is not None and len(reactions) != expected.reaction_count:
        errors.append(f"reaction_count expected {expected.reaction_count}, got {len(reactions)}")
    if expected.file_count is not None and len(files) != expected.file_count:
        errors.append(f"file_count expected {expected.file_count}, got {len(files)}")
    if expected.no_initial_comment:
        comments = [file.get("initial_comment") for file in files if "initial_comment" in file]
        if comments:
            errors.append(f"expected no initial_comment on uploaded files, got {comments!r}")
    if expected.uploaded_text_contains is not None:
        uploaded = "\n".join(str(file.get("_voice_text", "")) for file in files)
        if expected.uploaded_text_contains not in uploaded:
            errors.append(f"uploaded voice text did not contain {expected.uploaded_text_contains!r}: {uploaded!r}")
    if expected.reply_in_thread is not None and messages:
        in_thread = messages[0].get("thread_ts") is not None
        if in_thread != expected.reply_in_thread:
            errors.append(f"reply_in_thread expected {expected.reply_in_thread}, got {in_thread}")
    if expected.text_contains is not None:
        joined = "\n".join(str(message.get("text", "")) for message in messages)
        if expected.text_contains not in joined:
            errors.append(f"text did not contain {expected.text_contains!r}: {joined!r}")
        forbidden_web_hallucination = ["James Frank Hopkins", "Green Herndon", "James Walker Edrington"]
        for forbidden in forbidden_web_hallucination:
            if forbidden in joined and forbidden not in case.text:
                errors.append(f"reply included unsupported web-search detail {forbidden!r}: {joined!r}")
        if re.search(r"@?[UW][A-Z0-9]{8,}", joined):
            errors.append(f"reply exposed hidden Slack user id: {joined!r}")
        if re.search(r"@[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+", joined):
            errors.append(f"reply used fake @Full Name mention: {joined!r}")
        if joined.startswith('"') and joined.endswith('"'):
            errors.append(f"reply should not be wrapped in quotes: {joined!r}")
        if re.search(r"(?im)^\s*(sender|user|speaker|author|content|message|latest message|text|response|reply)\s*:", joined):
            errors.append(f"reply leaked prompt metadata labels: {joined!r}")
        if "Irdnennam" in joined and "irdnennam" not in case.text.lower():
            errors.append(f"reply mentioned absent Irdnennam: {joined!r}")
    if expected.reaction is not None:
        names = [reaction.get("name") for reaction in reactions]
        if expected.reaction not in names:
            errors.append(f"reaction expected {expected.reaction!r}, got {names!r}")
    if expected.memory_contains is not None:
        memory_text = "\n".join(bot.memory_item_text(item) for item in result["memory"].get("memories", []))
        if expected.memory_contains not in memory_text:
            errors.append(f"memory did not contain {expected.memory_contains!r}: {memory_text!r}")
    if expected.relationship_contains is not None:
        relationship_text = "\n".join(bot.relationship_item_text(item) for item in result["memory"].get("relationships", []))
        if expected.relationship_contains not in relationship_text:
            errors.append(f"relationship memory did not contain {expected.relationship_contains!r}: {relationship_text!r}")
    return errors


DEFAULT_CASES = [
    Case("direct casual greeting", "wee, welcome back", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("direct casual status", "wee, what's going on", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("no quoted full reply", "wee, quote regression", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("no absent person hallucination", "wee, absent person regression", expected=Expected("direct", True, 1, 0, 0, False, "Yeah, fair.")),
    Case("no hidden slack id in reply", "wee, slack id regression", expected=Expected("direct", True, 1, 0, 0, False, "Wee")),
    Case("no sender content metadata in reply", "wee, sender content regression", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("direct time tool", "wee, what time is it?", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("relative time tool", "wee, how long ago did we start testing this?", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("casual rn does not call time", "wee, whatever dude dont bother me rn", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("channel catchup", "wee, what did I miss?", expected=Expected("direct", True, 1, 0, 0, False, "Recently, people tested")),
    Case("profile lookup", "wee, who is <@U057QU46B25>?", expected=Expected("direct", True, 1, 0, 0, False, "Andre Patterson")),
    Case("public person profile misroute falls back to web", "wee, who is ishowspeed?", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("public person web search", "wee, search web who is ishowspeed?", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("general knowledge web search", "wee, what is the capital of Mongolia?", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("web search source limited", "wee, who founded sigma nu and do you think they would ever be late to fraternity events?", expected=Expected("direct", True, 1, 0, 0, False, "can't verify the founder names")),
    Case("thread summary", "wee, summarize this thread", surface="thread", thread_active=True, expected=Expected("direct", True, 1, 0, 0, True, "tool-backed reply")),
    Case("memory lookup", "wee, search memory for favorite color", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("memory update", "wee, remember structured favorite color", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply", memory_contains="Andre's favorite color is green")),
    Case("relationship memory lookup", "wee, search relationship memory for Ernesto and Andre", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("relationship memory update", "wee, remember relationship structured poker rivalry", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply", relationship_contains="Ernesto and Andre have a friendly rivalry about poker")),
    Case("relationship memory command", "wee, remember relationship: Ernesto and Andre have a friendly rivalry about poker", expected=Expected("direct", True, 1, 0, 0, False, "relationship memory", relationship_contains="Ernesto and Andre have a friendly rivalry about poker")),
    Case("automatic memory fact", "wee, Andre's favorite color is purple", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply", memory_contains="Andre's favorite color is purple.")),
    Case("automatic relationship memory fact", "wee, Ernesto and Andre are poker rivals", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply", relationship_contains="Ernesto and Andre are poker rivals.")),
    Case("automatic lowercase relationship fact", "wee, brios is flakey", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply", relationship_contains="Brios is flakey.")),
    Case("active threads", "wee, what active threads are there?", expected=Expected("direct", True, 1, 0, 0, False, "The active threads I see")),
    Case("channel search", "wee, did we talk about scopes recently?", expected=Expected("direct", True, 1, 0, 0, False, "scopes")),
    Case("quote recent message", "wee, quote recent reaction fix", expected=Expected("direct", True, 1, 0, 0, False, "reaction fix")),
    Case("message permalink", "wee, get the message link", expected=Expected("direct", True, 1, 0, 0, False, "https://fake.slack.com")),
    Case("inspect reactions", "wee, inspect reactions", expected=Expected("direct", True, 1, 0, 0, False, ":tada: x2")),
    Case("reminder", "wee, remind me to check the Slack bot logs tomorrow", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("reaction set", "wee, reaction set for this", expected=Expected("direct", True, 1, 3, 0, False, "normal conversational reply", reaction="tada")),
    Case("link preview", "wee, link preview https://example.com/article", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("link preview misroute rejected", "wee, link misroute without url", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("web search", "wee, search web for kokoro tts latest", expected=Expected("direct", True, 1, 0, 0, False, "tool-backed reply")),
    Case("web search misroute rejected", "wee, web misroute but just kidding", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("voice note disabled fallback", "wee, send a voice note", expected=Expected("direct", True, 1, 0, 0, False, "Voice notes are disabled")),
    Case("voice note upload", "wee, send a voice note", config_overrides={"voice_enabled": True}, expected=Expected("direct", True, 0, 0, 1, no_initial_comment=True, uploaded_text_contains="normal conversational reply")),
    Case("voice note failure stays terse", "wee, send a voice note", config_overrides={"voice_enabled": True, "voice_provider": "broken"}, expected=Expected("direct", True, 1, 0, 0, False, "voice-note issue")),
    Case("random voice instead of text", "wee, welcome back", config_overrides={"voice_enabled": True, "voice_response_chance": 1.0}, expected=Expected("direct", True, 0, 0, 1, no_initial_comment=True, uploaded_text_contains="normal conversational reply")),
    Case("ambient reaction", "we shipped it!", expected=Expected("ambient_router_candidate", True, 0, 1, 0, None, reaction="tada")),
    Case("ambient generic reaction diversified", "thank you for checking this", expected=Expected("ambient_router_candidate", True, 0, 1, 0)),
    Case("ambient fallback reaction", "small status update from me", config_overrides={"ambient_reaction_fallback_chance": 1.0}, expected=Expected("ambient_router_candidate", True, 0, 1, 0)),
    Case("ambient non-tool skipped", "small status update from me", expected=Expected("ambient_router_candidate", True, 0, 0, 0)),
    Case("passive mention skipped", "I asked wee about this earlier", expected=Expected("passive_name_mention", False, 0, 0, 0)),
    Case("explicit-only ambient skipped", "we shipped it!", channel="CANNOUNCE", config_overrides={"explicit_only_channels": ("announce",)}, expected=Expected("explicit_only_channel", False, 0, 0, 0)),
    Case("explicit-only direct allowed", "wee, welcome back", channel="CANNOUNCE", config_overrides={"explicit_only_channels": ("announce",)}, expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("low ambient channel sampled out", "we shipped it!", channel="CANNOUNCE", config_overrides={"ambient_channel_chances": {"announce": 0.0}}, expected=Expected("channel_ambient_sampled_out", False, 0, 0, 0)),
    Case("low ambient channel direct allowed", "wee, welcome back", channel="CANNOUNCE", config_overrides={"ambient_channel_chances": {"announce": 0.0}}, expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("bot ambient skipped", "we shipped it!", is_bot_message=True, expected=Expected("bot_ambient_suppressed", False, 0, 0, 0)),
    Case("bot direct allowed", "wee, what's going on", is_bot_message=True, expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("thread inactive skipped", "what do you think?", surface="thread", thread_active=False, expected=Expected("inactive_thread", False, 0, 0, 0)),
    Case("thread direct reply", "wee, what's going on", surface="thread", thread_active=False, expected=Expected("direct", True, 1, 0, 0, True, "normal conversational reply")),
    Case("thread ambient followup replies", "what do you think?", surface="thread", thread_active=True, expected=Expected("active_followup", True, 1, 0, 0, True, "normal conversational reply")),
    Case("thread short confusion followup replies", "What", surface="thread", thread_active=True, expected=Expected("active_thread_short_followup", True, 1, 0, 0, True, "normal conversational reply")),
    Case("thread passive mention skipped", "I asked wee earlier", surface="thread", thread_active=True, expected=Expected("passive_name_mention", False, 0, 0, 0)),
    Case("direct side reaction allowed", "Wee, Ernesto is not your best friend, he stole Lee's cookies the other day.", expected=Expected("direct", True, 1, 1, 0, False, "normal conversational reply", reaction="thumbsup")),
    Case("direct explicit reaction allowed", "Wee, please react to this", expected=Expected("direct", True, 1, 1, 0, False, "normal conversational reply", reaction="thumbsup")),
    Case("profile lookup misroute rejected", "Wee, Ernesto visited yesterday profile misroute", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("invalid tool rejected", "wee, wrong tool please", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("low confidence tool rejected", "wee, low confidence time", expected=Expected("direct", True, 1, 0, 0, False, "normal conversational reply")),
    Case("reaction sampled out", "we shipped it!", config_overrides={"reaction_response_chance": 0.0}, expected=Expected("ambient_router_candidate", True, 0, 0, 0)),
]


def run_default_suite(verbose: bool) -> int:
    failures = 0
    original_call_ollama = bot.call_ollama
    original_requests_get = bot.requests.get
    bot.call_ollama = fake_ollama
    bot.requests.get = fake_requests_get
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            for index, case in enumerate(DEFAULT_CASES, start=1):
                memory_path = Path(tmp) / f"{index}.memory.json"
                config = make_config(memory_path, **case.config_overrides)
                result = run_case(config, client, case, index)
                errors = check_result(case, result)
                status = "PASS" if not errors else "FAIL"
                print(f"{status} {case.name}: decision={result['decision']} messages={len(result['messages'])} reactions={len(result['reactions'])} files={len(result['files'])}")
                if verbose or errors:
                    print(json.dumps({k: v for k, v in result.items() if k != "memory"}, indent=2))
                for error in errors:
                    print(f"  - {error}")
                if errors:
                    failures += 1
    finally:
        bot.call_ollama = original_call_ollama
        bot.requests.get = original_requests_get
    print(f"\n{len(DEFAULT_CASES) - failures}/{len(DEFAULT_CASES)} passed")
    return 1 if failures else 0


def run_ad_hoc(messages: list[str]) -> int:
    original_call_ollama = bot.call_ollama
    original_requests_get = bot.requests.get
    bot.call_ollama = fake_ollama
    bot.requests.get = fake_requests_get
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "memory.json")
            client = FakeClient()
            for index, message in enumerate(messages, start=1):
                case = Case(f"ad hoc {index}", message)
                result = run_case(config, client, case, index)
                print(json.dumps({k: v for k, v in result.items() if k != "memory"}, indent=2))
    finally:
        bot.call_ollama = original_call_ollama
        bot.requests.get = original_requests_get
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("messages", nargs="*")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.messages:
        raise SystemExit(run_ad_hoc(args.messages))
    raise SystemExit(run_default_suite(args.verbose))


if __name__ == "__main__":
    main()
