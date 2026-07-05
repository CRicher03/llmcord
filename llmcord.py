import asyncio
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import io
import ipaddress
import logging
import os
import json
import random
import re
import sqlite3
from typing import Any, Literal, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup
import discord
from discord.app_commands import Choice
from discord.ext import commands
from discord.ui import LayoutView, TextDisplay
from docx import Document
from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI
from pypdf import PdfReader
import yaml

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

VISION_MODEL_TAGS = ("chat-latest", "claude", "gemini", "gemma", "gpt-4", "gpt-5", "gpt-latest", "grok-4", "llama", "vision", "vl")

EMBED_COLOR_COMPLETE = discord.Color.dark_green()
EMBED_COLOR_INCOMPLETE = discord.Color.orange()

STREAMING_INDICATOR = " âšª"
EDIT_DELAY_SECONDS = 1

MAX_MESSAGE_NODES = 500
SETTINGS_FILENAME = "channel_settings.json"
DATABASE_FILENAME = "llmcord.sqlite3"
URL_RE = re.compile(r"https?://[^\s<>()\]\}]+")

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

DEFAULT_MODULES = {
    "server_newspaper": {
        "enabled": False,
        "timezone": "America/New_York",
        "post_time": "00:00",
        "lookback_hours": 24,
        "max_messages_per_channel": 150,
        "max_total_messages": 800,
        "ignored_channel_ids": [],
        "model": None,
    },
    "mediator": {
        "enabled": True,
        "model": None,
        "cooldown_seconds": 60,
    },
    "guess_user": {
        "enabled": True,
        "mode": "opt_out",
        "round_timeout_seconds": 60,
        "allow_reuse_clues": False,
        "min_players": 3,
        "cooldown_seconds": 30,
        "lookback_days": 90,
        "max_messages_per_user": 300,
        "max_messages_per_channel": 1000,
        "ignored_channel_ids": [],
        "include_bots": False,
        "require_notice_before_scan": True,
        "store_raw_messages": False,
        "clue_safety_filter": True,
        "scan_delay_seconds": 0.5,
        "model": None,
    },
}

SERIOUS_MEDIATION_RE = re.compile(
    r"\b(abuse|assault|blackmail|custody|divorce|emergency|eviction|harassment|hospital|illegal|lawsuit|legal|"
    r"medical|mental health|police|restraining order|self[- ]?harm|suicide|threat|violence|weapon)\b",
    re.IGNORECASE,
)
REFUSE_MEDIATION_RE = re.compile(
    r"\b(abuse|assault|emergency|harassment|kill|self[- ]?harm|suicide|threat|violence|weapon)\b",
    re.IGNORECASE,
)
UNSAFE_CLUE_RE = re.compile(
    r"\b(abuse|address|anxiety|argu|bank|boyfriend|breakup|confess|custody|debt|depressed|depression|diagnos|"
    r"divorce|doctor|drama|ethnic|family|fired|gay|gender|girlfriend|hospital|illness|job|lawsuit|legal|"
    r"location|medical|mental|money|politic|race|religion|rent|school|sex|sexual|suicide|therapy|trauma|work)\b",
    re.IGNORECASE,
)


def parse_env_value(value: Optional[str]) -> Any:
    if value is None:
        return None

    stripped = value.strip()

    # Allow Render env vars like ADMIN_IDS=[123,456] or ADMIN_IDS=123,456
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    if "," in stripped:
        parts = [part.strip() for part in stripped.split(",") if part.strip()]
        return [int(part) if part.isdigit() else part for part in parts]

    return int(stripped) if stripped.isdigit() else value


def resolve_env(node: Any) -> Any:
    if isinstance(node, dict):
        return {key.removesuffix("_env"): parse_env_value(os.environ.get(value)) if key.endswith("_env") else resolve_env(value) for key, value in node.items()}
    return node


def get_config(filename: str = "config.yaml") -> dict[str, Any]:
    with open(filename, encoding="utf-8") as file:
        return resolve_env(yaml.safe_load(file))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_FILENAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS module_settings (
                guild_id INTEGER NOT NULL,
                module TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (guild_id, module, key)
            );

            CREATE TABLE IF NOT EXISTS module_ignored_channels (
                guild_id INTEGER NOT NULL,
                module TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, module, channel_id)
            );

            CREATE TABLE IF NOT EXISTS newspaper_runs (
                guild_id INTEGER NOT NULL,
                issue_date TEXT NOT NULL,
                posted_at TEXT NOT NULL,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, issue_date)
            );

            CREATE TABLE IF NOT EXISTS guess_user_optouts (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                opted_out_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS guess_user_clues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                clue TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                used_count INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS guess_user_scores (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS guess_user_scans (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_scan_at TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS guess_user_rounds (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                clue_id INTEGER NOT NULL,
                answer_user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            );
            """
        )


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def json_loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def get_module_config(loaded_config: dict[str, Any], module: str, guild_id: Optional[int] = None) -> dict[str, Any]:
    module_config = dict(DEFAULT_MODULES.get(module, {}))
    module_config.update((loaded_config.get("modules") or {}).get(module) or {})

    if guild_id is not None:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM module_settings WHERE guild_id = ? AND module = ?",
                (guild_id, module),
            ).fetchall()
            for row in rows:
                module_config[row["key"]] = json_loads(row["value"], row["value"])

            ignored_rows = conn.execute(
                "SELECT channel_id FROM module_ignored_channels WHERE guild_id = ? AND module = ?",
                (guild_id, module),
            ).fetchall()

        ignored_channel_ids = set(int(channel_id) for channel_id in module_config.get("ignored_channel_ids", []) or [])
        ignored_channel_ids.update(int(row["channel_id"]) for row in ignored_rows)
        module_config["ignored_channel_ids"] = sorted(ignored_channel_ids)

    return module_config


def set_module_setting(guild_id: int, module: str, key: str, value: Any) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO module_settings (guild_id, module, key, value) VALUES (?, ?, ?, ?)",
            (guild_id, module, key, json_dumps(value)),
        )


def get_module_setting(guild_id: int, module: str, key: str, default: Any = None) -> Any:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM module_settings WHERE guild_id = ? AND module = ? AND key = ?",
            (guild_id, module, key),
        ).fetchone()
    return json_loads(row["value"], default) if row else default


def set_ignored_channel(guild_id: int, module: str, channel_id: int, ignored: bool) -> None:
    with get_db() as conn:
        if ignored:
            conn.execute(
                "INSERT OR IGNORE INTO module_ignored_channels (guild_id, module, channel_id) VALUES (?, ?, ?)",
                (guild_id, module, channel_id),
            )
        else:
            conn.execute(
                "DELETE FROM module_ignored_channels WHERE guild_id = ? AND module = ? AND channel_id = ?",
                (guild_id, module, channel_id),
            )


def load_channel_settings(filename: str = SETTINGS_FILENAME) -> dict[str, Any]:
    if not os.path.exists(filename):
        return {"channels": {}}

    with open(filename, encoding="utf-8") as file:
        loaded_settings = json.load(file)

    loaded_settings.setdefault("channels", {})
    return loaded_settings


def save_channel_settings(filename: str = SETTINGS_FILENAME) -> None:
    with open(filename, "w", encoding="utf-8") as file:
        json.dump(channel_settings, file, indent=2, sort_keys=True)


def get_channel_key(channel: Any) -> Optional[str]:
    channel_id = getattr(channel, "id", None)
    return str(channel_id) if channel_id is not None else None


def get_channel_override(channel: Any, key: str) -> Optional[str]:
    channel_key = get_channel_key(channel)
    if channel_key is None:
        return None
    value = channel_settings.get("channels", {}).get(channel_key, {}).get(key)
    return value if isinstance(value, str) else None


def set_channel_override(channel: Any, key: str, value: Optional[str]) -> None:
    channel_key = get_channel_key(channel)
    if channel_key is None:
        return

    channel_values = channel_settings.setdefault("channels", {}).setdefault(channel_key, {})
    if value is None:
        channel_values.pop(key, None)
    else:
        channel_values[key] = value

    if not channel_values:
        channel_settings["channels"].pop(channel_key, None)

    save_channel_settings()


def get_effective_model(channel: Any, loaded_config: dict[str, Any]) -> str:
    model = get_channel_override(channel, "model")
    return model if model in loaded_config["models"] else curr_model


def get_personas(loaded_config: dict[str, Any]) -> dict[str, str]:
    personas = {"default": loaded_config.get("system_prompt", "") or ""}
    personas.update(loaded_config.get("personas") or {})
    return {name: prompt for name, prompt in personas.items() if isinstance(prompt, str)}


def get_effective_persona(channel: Any, loaded_config: dict[str, Any]) -> str:
    persona = get_channel_override(channel, "persona")
    return persona if persona in get_personas(loaded_config) else "default"


config = get_config()
init_db()
curr_model = next(iter(config["models"]))
channel_settings = load_channel_settings()

msg_nodes = {}
active_guess_rounds = {}
mediator_cooldowns = {}
guess_user_cooldowns = {}
newspaper_task: Optional[asyncio.Task] = None
last_task_time = 0

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
activity = discord.CustomActivity(name=(config.get("status_message") or "github.com/jakobdylanc/llmcord")[:128])
discord_bot = commands.Bot(intents=intents, activity=activity, command_prefix=None)

httpx_client = httpx.AsyncClient()


@dataclass
class MsgNode:
    role: Literal["user", "assistant"] = "assistant"

    text: Optional[str] = None
    images: list[dict[str, Any]] = field(default_factory=list)

    has_bad_attachments: bool = False
    has_bad_links: bool = False
    fetch_parent_failed: bool = False

    parent_msg: Optional[discord.Message] = None

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)



def interaction_is_private(interaction: discord.Interaction) -> bool:
    return interaction.channel is None or getattr(interaction.channel, "type", None) == discord.ChannelType.private


def is_admin_user(user_id: int, loaded_config: dict[str, Any]) -> bool:
    admin_ids = loaded_config.get("permissions", {}).get("users", {}).get("admin_ids", []) or []
    if isinstance(admin_ids, int):
        admin_ids = [admin_ids]
    return user_id in {int(admin_id) for admin_id in admin_ids}


def user_has_permission_for_interaction(interaction: discord.Interaction, loaded_config: dict[str, Any]) -> bool:
    permissions = loaded_config["permissions"]
    user_id = interaction.user.id

    if is_admin_user(user_id, loaded_config):
        return True

    allowed_user_ids = permissions["users"].get("allowed_ids", []) or []
    blocked_user_ids = permissions["users"].get("blocked_ids", []) or []
    allowed_role_ids = permissions["roles"].get("allowed_ids", []) or []
    blocked_role_ids = permissions["roles"].get("blocked_ids", []) or []
    allowed_channel_ids = permissions["channels"].get("allowed_ids", []) or []
    blocked_channel_ids = permissions["channels"].get("blocked_ids", []) or []

    role_ids = {role.id for role in getattr(interaction.user, "roles", ())}
    channel = interaction.channel
    channel_ids = set(filter(None, (
        getattr(channel, "id", None),
        getattr(channel, "parent_id", None),
        getattr(channel, "category_id", None),
    )))

    is_dm_or_group = interaction.guild is None
    allow_dms = loaded_config.get("allow_dms", True)

    allow_all_users = not allowed_user_ids if is_dm_or_group else not allowed_user_ids and not allowed_role_ids
    is_good_user = allow_all_users or user_id in allowed_user_ids or any(role_id in allowed_role_ids for role_id in role_ids)
    is_bad_user = not is_good_user or user_id in blocked_user_ids or any(role_id in blocked_role_ids for role_id in role_ids)

    allow_all_channels = not allowed_channel_ids
    is_good_channel = allow_dms if is_dm_or_group else allow_all_channels or any(channel_id in allowed_channel_ids for channel_id in channel_ids)
    is_bad_channel = not is_good_channel or any(channel_id in blocked_channel_ids for channel_id in channel_ids)

    return not is_bad_user and not is_bad_channel


def get_attachment_kind(attachment: discord.Attachment) -> Optional[Literal["text", "image", "pdf", "docx"]]:
    content_type = (attachment.content_type or "").lower()
    filename = attachment.filename.lower()

    if content_type.startswith("text") or filename.endswith((".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".csv", ".log")):
        return "text"
    if content_type.startswith("image"):
        return "image"
    if content_type == "application/pdf" or filename.endswith(".pdf"):
        return "pdf"
    if content_type == DOCX_CONTENT_TYPE or filename.endswith(".docx"):
        return "docx"
    return None


def normalize_extracted_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return normalize_extracted_text("\n\n".join(page.extract_text() or "" for page in reader.pages))


def extract_docx_text(data: bytes) -> str:
    document = Document(io.BytesIO(data))
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return normalize_extracted_text("\n".join(parts))


def extract_html_text(data: bytes) -> str:
    soup = BeautifulSoup(data, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    body = normalize_extracted_text(soup.get_text("\n"))
    return normalize_extracted_text("\n\n".join(part for part in (title, body) if part))


def extract_response_text(url: str, content_type: str, data: bytes) -> str:
    lowered_url = url.lower().split("?", 1)[0]
    lowered_type = content_type.lower()

    if "application/pdf" in lowered_type or lowered_url.endswith(".pdf"):
        return extract_pdf_text(data)
    if DOCX_CONTENT_TYPE in lowered_type or lowered_url.endswith(".docx"):
        return extract_docx_text(data)
    if "html" in lowered_type:
        return extract_html_text(data)
    if lowered_type.startswith("text/") or "json" in lowered_type or "xml" in lowered_type:
        return normalize_extracted_text(data.decode("utf-8", errors="replace"))
    return ""


def is_fetchable_url(url: str) -> bool:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        return False

    hostname = parsed_url.hostname.lower()
    if hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".local"):
        return False

    try:
        ip_address = ipaddress.ip_address(hostname)
    except ValueError:
        return True

    return not (ip_address.is_private or ip_address.is_loopback or ip_address.is_link_local or ip_address.is_multicast)


async def extract_url_texts(text: str, loaded_config: dict[str, Any]) -> tuple[list[str], bool]:
    max_urls = loaded_config.get("max_urls", 3)
    max_url_text = loaded_config.get("max_url_text", 15000)
    timeout = loaded_config.get("url_fetch_timeout", 10)
    urls = []
    had_failures = False

    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?\"'")
        if not is_fetchable_url(url):
            had_failures = True
            continue
        if url not in urls:
            urls.append(url)
        if len(urls) >= max_urls:
            break

    url_texts = []

    for url in urls:
        try:
            response = await httpx_client.get(
                url,
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": "llmcord/1.0"},
            )
            response.raise_for_status()

            extracted_text = await asyncio.to_thread(
                extract_response_text,
                str(response.url),
                response.headers.get("content-type", ""),
                response.content,
            )
            if extracted_text:
                url_texts.append(f"[URL: {url}]\n{extracted_text[:max_url_text]}")
            else:
                had_failures = True
        except Exception:
            logging.exception("Error fetching URL for message context: %s", url)
            had_failures = True

    return url_texts, had_failures


async def extract_attachment_text(attachment: discord.Attachment, response: httpx.Response, kind: str, loaded_config: dict[str, Any]) -> str:
    max_attachment_text = loaded_config.get("max_attachment_text", loaded_config.get("max_text", 100000))

    if kind == "text":
        return response.text[:max_attachment_text]
    if kind == "pdf":
        text = await asyncio.to_thread(extract_pdf_text, response.content)
        return f"[PDF: {attachment.filename}]\n{text[:max_attachment_text]}"
    if kind == "docx":
        text = await asyncio.to_thread(extract_docx_text, response.content)
        return f"[DOCX: {attachment.filename}]\n{text[:max_attachment_text]}"
    return ""


async def populate_msg_node(curr_msg: discord.Message, curr_node: MsgNode, loaded_config: dict[str, Any]) -> None:
    cleaned_content = curr_msg.content.removeprefix(discord_bot.user.mention).lstrip()
    curr_node.role = "assistant" if curr_msg.author == discord_bot.user else "user"

    attachment_kinds = [(att, kind) for att in curr_msg.attachments if (kind := get_attachment_kind(att))]
    attachment_responses = await asyncio.gather(
        *[httpx_client.get(att.url) for att, _ in attachment_kinds],
        return_exceptions=True,
    )

    attachment_texts = []
    curr_node.images = []
    bad_attachment_count = len(curr_msg.attachments) - len(attachment_kinds)

    for (attachment, kind), response in zip(attachment_kinds, attachment_responses):
        if isinstance(response, Exception):
            logging.warning("Error fetching attachment for message context: %s", response)
            bad_attachment_count += 1
            continue

        try:
            if kind == "image":
                curr_node.images.append(dict(type="image_url", image_url=dict(url=f"data:{attachment.content_type};base64,{b64encode(response.content).decode('utf-8')}")))
            else:
                attachment_text = await extract_attachment_text(attachment, response, kind, loaded_config)
                if attachment_text:
                    attachment_texts.append(attachment_text)
        except Exception:
            logging.exception("Error extracting attachment text")
            bad_attachment_count += 1

    url_texts = []
    if curr_node.role == "user" and cleaned_content:
        url_texts, curr_node.has_bad_links = await extract_url_texts(cleaned_content, loaded_config)

    curr_node.text = "\n".join(
        ([cleaned_content] if cleaned_content else [])
        + ["\n".join(filter(None, (embed.title, embed.description, embed.footer.text))) for embed in curr_msg.embeds]
        + [component.content for component in curr_msg.components if component.type == discord.ComponentType.text_display]
        + attachment_texts
        + url_texts
    )

    if curr_node.role == "user" and (curr_node.text or curr_node.images):
        curr_node.text = f"<@{curr_msg.author.id}>: {curr_node.text}"

    curr_node.has_bad_attachments = bad_attachment_count > 0


def node_to_message_content(curr_node: MsgNode, max_text: int, max_images: int) -> Any:
    text = (curr_node.text or "")[:max_text]

    if curr_node.images[:max_images]:
        return [dict(type="text", text=text)] + curr_node.images[:max_images]
    return text


def build_openai_client_and_kwargs(loaded_config: dict[str, Any], provider_slash_model: str, messages: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
    provider, model = provider_slash_model.removesuffix(":vision").split("/", 1)

    provider_config = loaded_config["providers"][provider]
    base_url = provider_config["base_url"]
    api_key = provider_config.get("api_key", "sk-no-key-required")
    openai_client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    model_parameters = loaded_config["models"].get(provider_slash_model, None)
    extra_headers = provider_config.get("extra_headers")
    extra_query = provider_config.get("extra_query")
    extra_body = (provider_config.get("extra_body") or {}) | (model_parameters or {}) or None

    return dict(
        client=openai_client,
        kwargs=dict(
            model=model,
            messages=messages,
            stream=stream,
            extra_headers=extra_headers,
            extra_query=extra_query,
            extra_body=extra_body,
        ),
    )


def get_system_prompt(loaded_config: dict[str, Any], persona: str = "default") -> str:
    system_prompt = get_personas(loaded_config).get(persona) or ""
    if system_prompt:
        now = datetime.now().astimezone()
        system_prompt = system_prompt.replace("{date}", now.strftime("%B %d %Y")).replace("{time}", now.strftime("%H:%M:%S %Z%z")).strip()
    return system_prompt


def append_system_prompt(messages: list[dict[str, Any]], loaded_config: dict[str, Any], persona: str = "default") -> None:
    if system_prompt := get_system_prompt(loaded_config, persona):
        messages.append(dict(role="system", content=system_prompt))


async def generate_nonstream_response(prompt: str, user_id: int, loaded_config: dict[str, Any], provider_slash_model: str, persona: str) -> str:
    messages = [dict(role="user", content=f"<@{user_id}>: {prompt[:loaded_config.get('max_text', 100000)]}")]
    append_system_prompt(messages, loaded_config, persona)

    client_and_kwargs = build_openai_client_and_kwargs(loaded_config, provider_slash_model, messages[::-1], stream=False)
    response = await client_and_kwargs["client"].chat.completions.create(**client_and_kwargs["kwargs"])
    return response.choices[0].message.content or ""


async def send_interaction_chunks(interaction: discord.Interaction, content: str, private: bool) -> None:
    max_len = 1900
    chunks = [content[i:i + max_len] for i in range(0, len(content), max_len)] or ["*(empty response)*"]
    for index, chunk in enumerate(chunks):
        if index == 0:
            await interaction.followup.send(chunk, ephemeral=private)
        else:
            await interaction.followup.send(chunk, ephemeral=private)


async def set_parent_msg(curr_msg: discord.Message, curr_node: MsgNode) -> None:
    try:
        if (
            curr_msg.reference == None
            and discord_bot.user.mention not in curr_msg.content
            and (prev_msg_in_channel := ([m async for m in curr_msg.channel.history(before=curr_msg, limit=1)] or [None])[0])
            and prev_msg_in_channel.type in (discord.MessageType.default, discord.MessageType.reply)
            and prev_msg_in_channel.author == (discord_bot.user if curr_msg.channel.type == discord.ChannelType.private else curr_msg.author)
        ):
            curr_node.parent_msg = prev_msg_in_channel
        else:
            is_public_thread = curr_msg.channel.type == discord.ChannelType.public_thread
            parent_is_thread_start = is_public_thread and curr_msg.reference == None and curr_msg.channel.parent.type == discord.ChannelType.text

            if parent_msg_id := curr_msg.channel.id if parent_is_thread_start else getattr(curr_msg.reference, "message_id", None):
                if parent_is_thread_start:
                    curr_node.parent_msg = curr_msg.channel.starter_message or await curr_msg.channel.parent.fetch_message(parent_msg_id)
                else:
                    curr_node.parent_msg = curr_msg.reference.cached_message or await curr_msg.channel.fetch_message(parent_msg_id)

    except (discord.NotFound, discord.HTTPException):
        logging.exception("Error fetching next message in the chain")
        curr_node.fetch_parent_failed = True


async def build_reply_chain_messages(start_msg: discord.Message, loaded_config: dict[str, Any], accept_images: bool) -> tuple[list[dict[str, Any]], set[str]]:
    max_text = loaded_config.get("max_text", 100000)
    max_images = loaded_config.get("max_images", 5) if accept_images else 0
    max_messages = loaded_config.get("max_messages", 25)

    messages = []
    user_warnings = set()
    curr_msg = start_msg

    while curr_msg != None and len(messages) < max_messages:
        curr_node = msg_nodes.setdefault(curr_msg.id, MsgNode())

        async with curr_node.lock:
            if curr_node.text == None:
                await populate_msg_node(curr_msg, curr_node, loaded_config)
                await set_parent_msg(curr_msg, curr_node)

            content = node_to_message_content(curr_node, max_text, max_images)

            if content != "":
                messages.append(dict(content=content, role=curr_node.role))

            if len(curr_node.text or "") > max_text:
                user_warnings.add(f"Warning: Max {max_text:,} characters per message")
            if len(curr_node.images) > max_images:
                user_warnings.add(f"Warning: Max {max_images} image{'' if max_images == 1 else 's'} per message" if max_images > 0 else "Warning: Can't see images")
            if curr_node.has_bad_attachments:
                user_warnings.add("Warning: Unsupported or unreadable attachments")
            if curr_node.has_bad_links:
                user_warnings.add("Warning: Some URLs could not be read")
            if curr_node.fetch_parent_failed or (curr_node.parent_msg != None and len(messages) == max_messages):
                user_warnings.add(f"Warning: Only using last {len(messages)} message{'' if len(messages) == 1 else 's'}")

            curr_msg = curr_node.parent_msg

    return messages, user_warnings


async def send_streaming_reply(start_msg: discord.Message, loaded_config: dict[str, Any], log_label: str = "Message received") -> None:
    global last_task_time

    provider_slash_model = get_effective_model(start_msg.channel, loaded_config)
    provider, model = provider_slash_model.removesuffix(":vision").split("/", 1)

    provider_config = loaded_config["providers"][provider]
    base_url = provider_config["base_url"]
    api_key = provider_config.get("api_key", "sk-no-key-required")
    openai_client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    model_parameters = loaded_config["models"].get(provider_slash_model, None)
    extra_headers = provider_config.get("extra_headers")
    extra_query = provider_config.get("extra_query")
    extra_body = (provider_config.get("extra_body") or {}) | (model_parameters or {}) or None

    accept_images = any(x in provider_slash_model.lower() for x in VISION_MODEL_TAGS)
    messages, user_warnings = await build_reply_chain_messages(start_msg, loaded_config, accept_images)

    logging.info(f"{log_label} (user ID: {start_msg.author.id}, attachments: {len(start_msg.attachments)}, conversation length: {len(messages)}, model: {provider_slash_model}):\n{start_msg.content}")

    append_system_prompt(messages, loaded_config, get_effective_persona(start_msg.channel, loaded_config))

    curr_content = finish_reason = None
    response_msgs = []
    response_contents = []

    openai_kwargs = dict(model=model, messages=messages[::-1], stream=True, extra_headers=extra_headers, extra_query=extra_query, extra_body=extra_body)

    if use_plain_responses := loaded_config.get("use_plain_responses", False):
        max_message_length = 4000
    else:
        max_message_length = 4096 - len(STREAMING_INDICATOR)
        embed = discord.Embed.from_dict(dict(fields=[dict(name=warning, value="", inline=False) for warning in sorted(user_warnings)]))

    async def reply_helper(**reply_kwargs) -> None:
        reply_target = start_msg if not response_msgs else response_msgs[-1]
        response_msg = await reply_target.reply(**reply_kwargs)
        response_msgs.append(response_msg)

        msg_nodes[response_msg.id] = MsgNode(parent_msg=start_msg)
        await msg_nodes[response_msg.id].lock.acquire()

    try:
        async with start_msg.channel.typing():
            async for chunk in await openai_client.chat.completions.create(**openai_kwargs):
                if finish_reason != None:
                    break

                if not (choice := chunk.choices[0] if chunk.choices else None):
                    continue

                finish_reason = choice.finish_reason

                prev_content = curr_content or ""
                curr_content = choice.delta.content or ""

                new_content = prev_content if finish_reason == None else (prev_content + curr_content)

                if response_contents == [] and new_content == "":
                    continue

                if start_next_msg := response_contents == [] or len(response_contents[-1] + new_content) > max_message_length:
                    response_contents.append("")

                response_contents[-1] += new_content

                if not use_plain_responses:
                    time_delta = datetime.now().timestamp() - last_task_time

                    ready_to_edit = time_delta >= EDIT_DELAY_SECONDS
                    msg_split_incoming = finish_reason == None and len(response_contents[-1] + curr_content) > max_message_length
                    is_final_edit = finish_reason != None or msg_split_incoming
                    is_good_finish = finish_reason != None and finish_reason.lower() in ("stop", "end_turn")

                    if start_next_msg or ready_to_edit or is_final_edit:
                        embed.description = response_contents[-1] if is_final_edit else (response_contents[-1] + STREAMING_INDICATOR)
                        embed.color = EMBED_COLOR_COMPLETE if msg_split_incoming or is_good_finish else EMBED_COLOR_INCOMPLETE

                        if start_next_msg:
                            await reply_helper(embed=embed, silent=True)
                        else:
                            await asyncio.sleep(EDIT_DELAY_SECONDS - time_delta)
                            await response_msgs[-1].edit(embed=embed)

                        last_task_time = datetime.now().timestamp()

            if use_plain_responses:
                for content in response_contents:
                    await reply_helper(view=LayoutView().add_item(TextDisplay(content=content)))

    except Exception:
        logging.exception("Error while generating response")

    for response_msg in response_msgs:
        msg_nodes[response_msg.id].text = "".join(response_contents)
        msg_nodes[response_msg.id].lock.release()

    if (num_nodes := len(msg_nodes)) > MAX_MESSAGE_NODES:
        for msg_id in sorted(msg_nodes.keys())[: num_nodes - MAX_MESSAGE_NODES]:
            async with msg_nodes.setdefault(msg_id, MsgNode()).lock:
                msg_nodes.pop(msg_id, None)


async def get_referenced_message(message: discord.Message) -> Optional[discord.Message]:
    if node := msg_nodes.get(message.id):
        if node.parent_msg is not None:
            return node.parent_msg

    if not message.reference or not getattr(message.reference, "message_id", None):
        return None

    try:
        return message.reference.cached_message or await message.channel.fetch_message(message.reference.message_id)
    except (discord.NotFound, discord.HTTPException):
        return None


async def find_retry_target(channel: Any) -> Optional[discord.Message]:
    async for message in channel.history(limit=50):
        if message.author != discord_bot.user:
            continue

        seen_msg_ids = set()
        curr_msg = message

        while curr_msg and curr_msg.id not in seen_msg_ids:
            seen_msg_ids.add(curr_msg.id)
            parent_msg = await get_referenced_message(curr_msg)
            if parent_msg is None:
                break
            if parent_msg.author != discord_bot.user:
                return parent_msg
            curr_msg = parent_msg

    return None


async def build_recent_channel_messages(channel: Any, loaded_config: dict[str, Any], limit: int, accept_images: bool) -> tuple[list[dict[str, Any]], set[str]]:
    max_text = loaded_config.get("max_text", 100000)
    max_images = loaded_config.get("max_images", 5) if accept_images else 0
    messages = []
    user_warnings = set()

    async for curr_msg in channel.history(limit=limit):
        if curr_msg.type not in (discord.MessageType.default, discord.MessageType.reply):
            continue

        curr_node = msg_nodes.setdefault(curr_msg.id, MsgNode())

        async with curr_node.lock:
            if curr_node.text == None:
                await populate_msg_node(curr_msg, curr_node, loaded_config)

            content = node_to_message_content(curr_node, max_text, max_images)
            if content != "":
                messages.append(dict(content=content, role=curr_node.role))

            if curr_node.has_bad_attachments:
                user_warnings.add("Warning: Some attachments could not be read")
            if curr_node.has_bad_links:
                user_warnings.add("Warning: Some URLs could not be read")

    return messages, user_warnings


def require_guild(interaction: discord.Interaction) -> Optional[discord.Guild]:
    return interaction.guild


async def require_admin_interaction(interaction: discord.Interaction, loaded_config: dict[str, Any]) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return False
    if not is_admin_user(interaction.user.id, loaded_config):
        await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
        return False
    return True


def module_model(loaded_config: dict[str, Any], module_config: dict[str, Any], channel: Any) -> str:
    configured_model = module_config.get("model")
    return configured_model if configured_model in loaded_config["models"] else get_effective_model(channel, loaded_config)


async def generate_module_text(loaded_config: dict[str, Any], provider_slash_model: str, messages: list[dict[str, Any]]) -> str:
    client_and_kwargs = build_openai_client_and_kwargs(loaded_config, provider_slash_model, messages, stream=False)
    response = await client_and_kwargs["client"].chat.completions.create(**client_and_kwargs["kwargs"])
    return response.choices[0].message.content or ""


async def send_channel_chunks(channel: Any, content: str) -> list[discord.Message]:
    chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)] or ["*(empty response)*"]
    sent_messages = []
    for chunk in chunks:
        sent_messages.append(await channel.send(chunk))
    return sent_messages


def is_public_server_text_channel(channel: Any, ignored_channel_ids: set[int]) -> bool:
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
        return False
    if not isinstance(channel, discord.TextChannel):
        return False
    if channel.id in ignored_channel_ids:
        return False

    guild = channel.guild
    bot_member = guild.me
    if bot_member is None:
        return False

    bot_perms = channel.permissions_for(bot_member)
    everyone_perms = channel.permissions_for(guild.default_role)

    return bool(
        bot_perms.view_channel
        and bot_perms.read_message_history
        and everyone_perms.view_channel
    )


def safe_message_text(message: discord.Message, max_len: int = 500) -> str:
    content = normalize_extracted_text(message.content)
    content = URL_RE.sub("[link]", content)
    content = re.sub(r"<@!?\d+>", "@user", content)
    content = re.sub(r"<#\d+>", "#channel", content)
    return content[:max_len]


async def collect_public_activity(
    guild: discord.Guild,
    module_config: dict[str, Any],
    since: datetime,
    max_total_messages: int,
) -> tuple[list[str], dict[str, int]]:
    ignored_channel_ids = set(int(channel_id) for channel_id in module_config.get("ignored_channel_ids", []) or [])
    max_per_channel = int(module_config.get("max_messages_per_channel", 150))
    activity_lines = []
    channel_counts = {}

    for channel in guild.text_channels:
        if not is_public_server_text_channel(channel, ignored_channel_ids):
            continue

        channel_count = 0
        try:
            async for message in channel.history(after=since, limit=max_per_channel, oldest_first=True):
                if message.author.bot or not message.content:
                    continue
                text = safe_message_text(message)
                if not text:
                    continue

                activity_lines.append(f"#{channel.name} | {message.author.display_name}: {text}")
                channel_count += 1

                if len(activity_lines) >= max_total_messages:
                    break
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Skipping channel during activity collection: %s", channel.id)

        if channel_count:
            channel_counts[channel.name] = channel_count

        if len(activity_lines) >= max_total_messages:
            break

    return activity_lines, channel_counts


def newspaper_issue_date(timezone_name: str) -> str:
    try:
        tzinfo = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tzinfo = ZoneInfo("America/New_York")
    return datetime.now(tzinfo).date().isoformat()


def newspaper_already_posted(guild_id: int, issue_date: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM newspaper_runs WHERE guild_id = ? AND issue_date = ?",
            (guild_id, issue_date),
        ).fetchone()
    return row is not None


def record_newspaper_run(guild_id: int, issue_date: str, channel_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO newspaper_runs (guild_id, issue_date, posted_at, channel_id) VALUES (?, ?, ?, ?)",
            (guild_id, issue_date, now_iso(), channel_id),
        )


async def generate_newspaper(guild: discord.Guild, channel: discord.TextChannel, loaded_config: dict[str, Any]) -> str:
    module_config = get_module_config(loaded_config, "server_newspaper", guild.id)
    timezone_name = module_config.get("timezone", "America/New_York")

    try:
        tzinfo = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tzinfo = ZoneInfo("America/New_York")
        timezone_name = "America/New_York"

    lookback_hours = int(module_config.get("lookback_hours", 24))
    since = datetime.now(tzinfo).astimezone() - timedelta(hours=lookback_hours)
    activity_lines, channel_counts = await collect_public_activity(
        guild,
        module_config,
        since,
        int(module_config.get("max_total_messages", 800)),
    )

    active_channels = ", ".join(f"#{name} ({count})" for name, count in sorted(channel_counts.items(), key=lambda item: item[1], reverse=True)[:8])
    if not activity_lines:
        activity_lines = ["Quiet day: not enough public, readable messages were available for a full edition."]

    prompt = (
        "Write a fun, general-audience daily Discord server newspaper from the public activity below. "
        "Do not include sensitive personal information, drama, insults, private details, or anything embarrassing. "
        "Avoid direct quotes unless harmless and public; prefer paraphrase. If activity is low, make a short quiet-day edition. "
        "Use these sections: Headline of the Day, Top Stories, Funniest/Most Notable Moments, Most Active Channels, "
        "Poll or Question of the Day, Upcoming Events or Reminders if available, Quote of the Day only if clearly appropriate, Closing Line.\n\n"
        f"Server: {guild.name}\nTimezone: {timezone_name}\nMost active channels by message count: {active_channels or 'None'}\n\n"
        "Public activity:\n" + "\n".join(activity_lines[-int(module_config.get("max_total_messages", 800)):])
    )

    messages = [
        dict(role="system", content="You are a safe, upbeat, family-friendly community newspaper editor for a Discord server."),
        dict(role="user", content=prompt),
    ]
    return await generate_module_text(loaded_config, module_model(loaded_config, module_config, channel), messages)


async def maybe_post_newspaper_for_guild(guild: discord.Guild, loaded_config: dict[str, Any], force: bool = False) -> bool:
    module_config = get_module_config(loaded_config, "server_newspaper", guild.id)
    if not force and not module_config.get("enabled", False):
        return False

    output_channel_id = get_module_setting(guild.id, "server_newspaper", "output_channel_id")
    if not output_channel_id:
        return False

    channel = guild.get_channel(int(output_channel_id))
    if not isinstance(channel, discord.TextChannel):
        return False

    bot_perms = channel.permissions_for(guild.me)
    if not (bot_perms.view_channel and bot_perms.send_messages and bot_perms.read_message_history):
        return False

    timezone_name = module_config.get("timezone", "America/New_York")
    issue_date = newspaper_issue_date(timezone_name)
    if not force and newspaper_already_posted(guild.id, issue_date):
        return False

    newspaper = await generate_newspaper(guild, channel, loaded_config)
    await send_channel_chunks(channel, newspaper)
    record_newspaper_run(guild.id, issue_date, channel.id)
    return True


async def newspaper_scheduler() -> None:
    await discord_bot.wait_until_ready()

    while not discord_bot.is_closed():
        try:
            loaded_config = await asyncio.to_thread(get_config)

            for guild in discord_bot.guilds:
                module_config = get_module_config(loaded_config, "server_newspaper", guild.id)
                if not module_config.get("enabled", False):
                    continue

                timezone_name = module_config.get("timezone", "America/New_York")
                post_time = str(module_config.get("post_time", "00:00"))
                try:
                    tzinfo = ZoneInfo(timezone_name)
                except ZoneInfoNotFoundError:
                    tzinfo = ZoneInfo("America/New_York")

                now_local = datetime.now(tzinfo)
                try:
                    hour, minute = [int(part) for part in post_time.split(":", 1)]
                except ValueError:
                    hour, minute = 0, 0

                scheduled_today = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if now_local >= scheduled_today:
                    await maybe_post_newspaper_for_guild(guild, loaded_config)
        except Exception:
            logging.exception("Error in newspaper scheduler")

        await asyncio.sleep(60)


def mediator_high_stakes_text(parts: list[str]) -> str:
    return "\n".join(part for part in parts if part)


async def run_mediator(interaction: discord.Interaction, topic: str, side_a: Optional[str] = None, side_b: Optional[str] = None) -> str:
    loaded_config = await asyncio.to_thread(get_config)
    module_config = get_module_config(loaded_config, "mediator", interaction.guild.id if interaction.guild else None)
    if not module_config.get("enabled", True):
        return "The mediator module is disabled here."

    combined = mediator_high_stakes_text([topic, side_a or "", side_b or ""])
    if REFUSE_MEDIATION_RE.search(combined):
        return (
            "I can't mediate situations involving threats, violence, self-harm, abuse, harassment, or emergencies. "
            "Please involve server moderators or appropriate real-world help."
        )

    disclaimer = ""
    if SERIOUS_MEDIATION_RE.search(combined):
        disclaimer = (
            "Note: this sounds potentially high-stakes. I can help organize thoughts, but this is not legal, medical, "
            "mental health, emergency, or professional advice. Consider involving a qualified person or moderator.\n\n"
        )

    prompt = (
        "Mediate this Discord disagreement neutrally. Do not pick a winner unless explicitly asked. "
        "Use this format: Neutral summary, Side A's likely concern, Side B's likely concern, Common ground, "
        "Suggested compromise, Next step. Keep it calm, non-judgmental, and practical.\n\n"
        f"Topic: {topic}\nSide A: {side_a or 'Not provided'}\nSide B: {side_b or 'Not provided'}"
    )
    messages = [
        dict(role="system", content="You are a calm, neutral mediator for low-stakes Discord disagreements and planning conflicts."),
        dict(role="user", content=prompt),
    ]
    provider_slash_model = module_model(loaded_config, module_config, interaction.channel)
    return disclaimer + await generate_module_text(loaded_config, provider_slash_model, messages)


def guess_user_enabled(guild_id: int, loaded_config: dict[str, Any]) -> bool:
    return bool(get_module_config(loaded_config, "guess_user", guild_id).get("enabled", True))


def guess_user_is_opted_out(guild_id: int, user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM guess_user_optouts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return row is not None


def set_guess_user_optout(guild_id: int, user_id: int, opted_out: bool) -> None:
    with get_db() as conn:
        if opted_out:
            conn.execute(
                "INSERT OR REPLACE INTO guess_user_optouts (guild_id, user_id, opted_out_at) VALUES (?, ?, ?)",
                (guild_id, user_id, now_iso()),
            )
            conn.execute("UPDATE guess_user_clues SET active = 0 WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        else:
            conn.execute("DELETE FROM guess_user_optouts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))


def delete_guess_user_data(guild_id: int, user_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM guess_user_clues WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        conn.execute("DELETE FROM guess_user_scores WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        conn.execute("DELETE FROM guess_user_scans WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))


async def collect_guess_user_messages(guild: discord.Guild, user: Any, module_config: dict[str, Any], incremental: bool = True) -> list[str]:
    ignored_channel_ids = set(int(channel_id) for channel_id in module_config.get("ignored_channel_ids", []) or [])
    lookback_days = int(module_config.get("lookback_days", 90))
    max_per_user = int(module_config.get("max_messages_per_user", 300))
    max_per_channel = int(module_config.get("max_messages_per_channel", 1000))
    since = datetime.now().astimezone() - timedelta(days=lookback_days)

    if incremental:
        with get_db() as conn:
            row = conn.execute(
                "SELECT last_scan_at FROM guess_user_scans WHERE guild_id = ? AND user_id = ?",
                (guild.id, user.id),
            ).fetchone()
        if row:
            try:
                since = max(since, datetime.fromisoformat(row["last_scan_at"]))
            except ValueError:
                pass

    samples = []
    for channel in guild.text_channels:
        if not is_public_server_text_channel(channel, ignored_channel_ids):
            continue

        seen_in_channel = 0
        try:
            async for message in channel.history(after=since, limit=max_per_channel, oldest_first=False):
                if message.author.id != user.id or not message.content:
                    continue
                text = safe_message_text(message, max_len=300)
                if text and not UNSAFE_CLUE_RE.search(text):
                    samples.append(text)
                seen_in_channel += 1
                if len(samples) >= max_per_user or seen_in_channel >= max_per_channel:
                    break
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Skipping channel during Guess the User scan: %s", channel.id)

        if len(samples) >= max_per_user:
            break

        await asyncio.sleep(float(module_config.get("scan_delay_seconds", 0.5)))

    return samples


def parse_clue_json(raw_text: str) -> list[str]:
    text = raw_text.strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        text = match.group(0)

    parsed = json_loads(text, [])
    if isinstance(parsed, dict):
        parsed = parsed.get("clues", [])
    if not isinstance(parsed, list):
        return []

    clues = []
    for clue in parsed:
        if isinstance(clue, str):
            cleaned = normalize_extracted_text(clue)
            if 15 <= len(cleaned) <= 160 and not UNSAFE_CLUE_RE.search(cleaned):
                clues.append(cleaned)
    return clues[:8]


async def generate_guess_user_clues(guild: discord.Guild, user: Any, samples: list[str], loaded_config: dict[str, Any]) -> list[str]:
    if not samples:
        return []

    module_config = get_module_config(loaded_config, "guess_user", guild.id)
    prompt = (
        "Generate 3 to 8 safe, vague, general-audience Guess the User clues from these public Discord messages. "
        "Return JSON only: an array of strings. Do not mention private, sensitive, embarrassing, identity, health, "
        "relationship, money, legal, workplace, school, family, political, religious, location, drama, argument, or venting details. "
        "Prefer harmless repeated hobbies, games, media, catchphrases, posting habits, and light server-safe topics. "
        "If unsure, omit the clue. Do not identify the user by name.\n\n"
        "Messages:\n" + "\n".join(f"- {sample}" for sample in samples[-120:])
    )
    messages = [
        dict(role="system", content="You generate only safe, non-sensitive party-game clues as JSON."),
        dict(role="user", content=prompt),
    ]
    raw_text = await generate_module_text(loaded_config, module_model(loaded_config, module_config, None), messages)
    return parse_clue_json(raw_text)


def store_guess_user_clues(guild_id: int, user_id: int, clues: list[str], message_count: int) -> int:
    inserted = 0
    with get_db() as conn:
        for clue in clues:
            existing = conn.execute(
                "SELECT 1 FROM guess_user_clues WHERE guild_id = ? AND user_id = ? AND clue = ? AND active = 1",
                (guild_id, user_id, clue),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO guess_user_clues (guild_id, user_id, clue, created_at) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, clue, now_iso()),
            )
            inserted += 1

        conn.execute(
            "INSERT OR REPLACE INTO guess_user_scans (guild_id, user_id, last_scan_at, message_count) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, now_iso(), message_count),
        )
    return inserted


async def scan_guess_user(guild: discord.Guild, user: Any, loaded_config: dict[str, Any], incremental: bool = True) -> tuple[int, int]:
    module_config = get_module_config(loaded_config, "guess_user", guild.id)
    if user.bot and not module_config.get("include_bots", False):
        return 0, 0
    if guess_user_is_opted_out(guild.id, user.id):
        return 0, 0

    samples = await collect_guess_user_messages(guild, user, module_config, incremental=incremental)
    clues = await generate_guess_user_clues(guild, user, samples, loaded_config)
    inserted = store_guess_user_clues(guild.id, user.id, clues, len(samples))
    return inserted, len(samples)


def get_random_guess_clue(guild: discord.Guild, loaded_config: dict[str, Any]) -> Optional[sqlite3.Row]:
    module_config = get_module_config(loaded_config, "guess_user", guild.id)
    allow_reuse = bool(module_config.get("allow_reuse_clues", False))
    reuse_clause = "" if allow_reuse else "AND used_count = 0"

    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT clues.*
                FROM guess_user_clues AS clues
                LEFT JOIN guess_user_optouts AS optouts
                    ON optouts.guild_id = clues.guild_id AND optouts.user_id = clues.user_id
                WHERE clues.guild_id = ?
                    AND clues.active = 1
                    AND optouts.user_id IS NULL
                    {reuse_clause}
                ORDER BY RANDOM()
                LIMIT 1""",
            (guild.id,),
        ).fetchall()
    return rows[0] if rows else None


def active_clue_user_count(guild: discord.Guild) -> int:
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT clues.user_id) AS count
                FROM guess_user_clues AS clues
                LEFT JOIN guess_user_optouts AS optouts
                    ON optouts.guild_id = clues.guild_id AND optouts.user_id = clues.user_id
                WHERE clues.guild_id = ?
                    AND clues.active = 1
                    AND optouts.user_id IS NULL""",
            (guild.id,),
        ).fetchone()
    return int(row["count"] if row else 0)


async def guild_has_member(guild: discord.Guild, user_id: int) -> bool:
    if guild.get_member(user_id) is not None:
        return True

    try:
        await guild.fetch_member(user_id)
        return True
    except discord.NotFound:
        return False
    except (discord.Forbidden, discord.HTTPException):
        return True


def deactivate_guess_user(guild_id: int, user_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE guess_user_clues SET active = 0 WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )


async def finish_guess_round(guild_id: int, channel_id: int, reveal: bool = True, winner_id: Optional[int] = None) -> None:
    round_info = active_guess_rounds.pop((guild_id, channel_id), None)
    if not round_info:
        return

    guild = discord_bot.get_guild(guild_id)
    channel = guild.get_channel(channel_id) if guild else None
    answer_user_id = round_info["answer_user_id"]
    clue_id = round_info["clue_id"]

    with get_db() as conn:
        conn.execute(
            "UPDATE guess_user_rounds SET status = ? WHERE guild_id = ? AND channel_id = ?",
            ("complete", guild_id, channel_id),
        )
        conn.execute(
            "UPDATE guess_user_clues SET last_used_at = ?, used_count = used_count + 1 WHERE id = ?",
            (now_iso(), clue_id),
        )

    if reveal and channel:
        answer_member = guild.get_member(answer_user_id) if guild else None
        answer = answer_member.mention if answer_member else f"<@{answer_user_id}>"
        if winner_id:
            await channel.send(f"Correct, <@{winner_id}>! The answer was {answer}.")
        else:
            await channel.send(f"Time! The answer was {answer}.")


newspaper_group = discord.app_commands.Group(name="newspaper", description="Daily server newspaper controls")
mediator_group = discord.app_commands.Group(name="mediator", description="AI mediator module controls")
guessuser_group = discord.app_commands.Group(name="guessuser", description="Guess the User party game")


@newspaper_group.command(name="generate", description="Generate today's server newspaper now")
async def newspaper_generate(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return

    await interaction.response.defer(thinking=True)
    posted = await maybe_post_newspaper_for_guild(interaction.guild, loaded_config, force=True)
    await interaction.followup.send("Newspaper generated." if posted else "I could not generate the newspaper. Check the output channel and permissions.", ephemeral=True)


@newspaper_group.command(name="set-channel", description="Set the newspaper output channel")
async def newspaper_set_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return

    set_module_setting(interaction.guild.id, "server_newspaper", "output_channel_id", channel.id)
    await interaction.response.send_message(f"Newspaper output channel set to {channel.mention}.", ephemeral=True)


@newspaper_group.command(name="enable", description="Enable the daily server newspaper")
async def newspaper_enable(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return

    set_module_setting(interaction.guild.id, "server_newspaper", "enabled", True)
    await interaction.response.send_message("Daily newspaper enabled.", ephemeral=True)


@newspaper_group.command(name="disable", description="Disable the daily server newspaper")
async def newspaper_disable(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return

    set_module_setting(interaction.guild.id, "server_newspaper", "enabled", False)
    await interaction.response.send_message("Daily newspaper disabled.", ephemeral=True)


@newspaper_group.command(name="ignore-channel", description="Exclude a channel from newspaper summaries")
async def newspaper_ignore_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return

    set_ignored_channel(interaction.guild.id, "server_newspaper", channel.id, True)
    await interaction.response.send_message(f"{channel.mention} will be ignored by the newspaper.", ephemeral=True)


@newspaper_group.command(name="unignore-channel", description="Include a channel in newspaper summaries again")
async def newspaper_unignore_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return

    set_ignored_channel(interaction.guild.id, "server_newspaper", channel.id, False)
    await interaction.response.send_message(f"{channel.mention} can be included in the newspaper again.", ephemeral=True)


@newspaper_group.command(name="status", description="Show newspaper settings")
async def newspaper_status(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    module_config = get_module_config(loaded_config, "server_newspaper", interaction.guild.id)
    output_channel_id = get_module_setting(interaction.guild.id, "server_newspaper", "output_channel_id")
    output_channel = interaction.guild.get_channel(int(output_channel_id)) if output_channel_id else None
    issue_date = newspaper_issue_date(module_config.get("timezone", "America/New_York"))

    await interaction.response.send_message(
        "\n".join(
            [
                f"Enabled: `{bool(module_config.get('enabled', False))}`",
                f"Output channel: {output_channel.mention if output_channel else '`not set`'}",
                f"Timezone: `{module_config.get('timezone', 'America/New_York')}`",
                f"Post time: `{module_config.get('post_time', '00:00')}`",
                f"Posted today: `{newspaper_already_posted(interaction.guild.id, issue_date)}`",
                f"Ignored channels: `{len(module_config.get('ignored_channel_ids', []))}`",
            ]
        ),
        ephemeral=True,
    )


@discord.app_commands.allowed_installs(guilds=True, users=False)
@discord.app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@discord.app_commands.describe(topic="The disagreement, planning conflict, or decision to mediate", side_a="Optional side A", side_b="Optional side B")
@discord_bot.tree.command(name="mediate", description="Get a neutral AI mediation summary")
async def mediate_command(interaction: discord.Interaction, topic: str, side_a: Optional[str] = None, side_b: Optional[str] = None) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not user_has_permission_for_interaction(interaction, loaded_config):
        await interaction.response.send_message("You don't have permission to use this bot here.", ephemeral=True)
        return

    module_config = get_module_config(loaded_config, "mediator", interaction.guild.id if interaction.guild else None)
    cooldown_seconds = int(module_config.get("cooldown_seconds", 60))
    cooldown_key = (interaction.guild.id if interaction.guild else 0, interaction.user.id)
    now_ts = datetime.now().timestamp()
    next_allowed = mediator_cooldowns.get(cooldown_key, 0)
    if now_ts < next_allowed:
        await interaction.response.send_message(f"Slow down a little. Try again in {int(next_allowed - now_ts)} seconds.", ephemeral=True)
        return

    mediator_cooldowns[cooldown_key] = now_ts + cooldown_seconds
    await interaction.response.defer(thinking=True)
    output = await run_mediator(interaction, topic, side_a, side_b)
    await send_interaction_chunks(interaction, output, private=False)


@mediator_group.command(name="enable", description="Enable the mediator module")
async def mediator_enable(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    set_module_setting(interaction.guild.id, "mediator", "enabled", True)
    await interaction.response.send_message("Mediator enabled.", ephemeral=True)


@mediator_group.command(name="disable", description="Disable the mediator module")
async def mediator_disable(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    set_module_setting(interaction.guild.id, "mediator", "enabled", False)
    await interaction.response.send_message("Mediator disabled.", ephemeral=True)


@mediator_group.command(name="status", description="Show mediator module settings")
async def mediator_status(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    module_config = get_module_config(loaded_config, "mediator", interaction.guild.id)
    await interaction.response.send_message(
        f"Enabled: `{bool(module_config.get('enabled', True))}`\nCooldown: `{module_config.get('cooldown_seconds', 60)}s`\nModel: `{module_config.get('model') or 'channel default'}`",
        ephemeral=True,
    )


@guessuser_group.command(name="enable", description="Enable Guess the User")
async def guessuser_enable(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    set_module_setting(interaction.guild.id, "guess_user", "enabled", True)
    await interaction.response.send_message("Guess the User enabled.", ephemeral=True)


@guessuser_group.command(name="disable", description="Disable Guess the User")
async def guessuser_disable(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    set_module_setting(interaction.guild.id, "guess_user", "enabled", False)
    await interaction.response.send_message("Guess the User disabled.", ephemeral=True)


@guessuser_group.command(name="privacy", description="Explain Guess the User privacy controls")
async def guessuser_privacy(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Guess the User uses only public server messages from channels the bot can read. It avoids private/mod-only channels, ignored channels, bots, deleted messages, and sensitive topics. "
        "It stores generated safe clues and lightweight metadata, not raw message history. Use `/guessuser opt-out` anytime, and `/guessuser delete-my-data` to remove your clues, scan metadata, and score.",
        ephemeral=True,
    )


@guessuser_group.command(name="opt-out", description="Opt out of Guess the User")
async def guessuser_opt_out(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    set_guess_user_optout(interaction.guild.id, interaction.user.id, True)
    await interaction.response.send_message("You are opted out, and your active clues were disabled.", ephemeral=True)


@guessuser_group.command(name="opt-in", description="Opt back into Guess the User")
async def guessuser_opt_in(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    set_guess_user_optout(interaction.guild.id, interaction.user.id, False)
    await interaction.response.send_message("You are opted in again. Admins can rescan you to generate fresh clues.", ephemeral=True)


@guessuser_group.command(name="delete-my-data", description="Delete your Guess the User data")
async def guessuser_delete_my_data(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    delete_guess_user_data(interaction.guild.id, interaction.user.id)
    set_guess_user_optout(interaction.guild.id, interaction.user.id, True)
    await interaction.response.send_message("Deleted your Guess the User clues, scan metadata, and score. You are also opted out.", ephemeral=True)


@guessuser_group.command(name="start", description="Start a Guess the User round")
async def guessuser_start(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not user_has_permission_for_interaction(interaction, loaded_config):
        await interaction.response.send_message("You don't have permission to use this bot here.", ephemeral=True)
        return
    if not guess_user_enabled(interaction.guild.id, loaded_config):
        await interaction.response.send_message("Guess the User is disabled here.", ephemeral=True)
        return

    module_config = get_module_config(loaded_config, "guess_user", interaction.guild.id)
    cooldown_key = (interaction.guild.id, interaction.user.id)
    now_ts = datetime.now().timestamp()
    next_allowed = guess_user_cooldowns.get(cooldown_key, 0)
    if now_ts < next_allowed:
        await interaction.response.send_message(f"Try again in {int(next_allowed - now_ts)} seconds.", ephemeral=True)
        return
    guess_user_cooldowns[cooldown_key] = now_ts + int(module_config.get("cooldown_seconds", 30))

    if active_clue_user_count(interaction.guild) < int(module_config.get("min_players", 3)):
        await interaction.response.send_message("Not enough scanned, opted-in users have clues yet.", ephemeral=True)
        return

    clue = None
    for _ in range(5):
        candidate_clue = get_random_guess_clue(interaction.guild, loaded_config)
        if not candidate_clue:
            break
        if await guild_has_member(interaction.guild, int(candidate_clue["user_id"])):
            clue = candidate_clue
            break
        deactivate_guess_user(interaction.guild.id, int(candidate_clue["user_id"]))

    if not clue:
        await interaction.response.send_message("No available clues yet. An admin can run `/guessuser scan-server`.", ephemeral=True)
        return

    expires_at = datetime.now().astimezone() + timedelta(seconds=int(module_config.get("round_timeout_seconds", 60)))
    active_guess_rounds[(interaction.guild.id, interaction.channel.id)] = {
        "clue_id": clue["id"],
        "answer_user_id": clue["user_id"],
        "expires_at": expires_at,
    }
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO guess_user_rounds (guild_id, channel_id, clue_id, answer_user_id, started_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (interaction.guild.id, interaction.channel.id, clue["id"], clue["user_id"], now_iso(), expires_at.isoformat(), "active"),
        )

    await interaction.response.send_message(f"Guess the User:\n\n**Clue:** {clue['clue']}\n\nUse `/guessuser guess user:@someone`.")

    async def timeout_round() -> None:
        await asyncio.sleep(int(module_config.get("round_timeout_seconds", 60)))
        if (interaction.guild.id, interaction.channel.id) in active_guess_rounds:
            await finish_guess_round(interaction.guild.id, interaction.channel.id)

    asyncio.create_task(timeout_round())


@guessuser_group.command(name="guess", description="Guess who the current clue describes")
async def guessuser_guess(interaction: discord.Interaction, user: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    round_info = active_guess_rounds.get((interaction.guild.id, interaction.channel.id))
    if not round_info:
        await interaction.response.send_message("There is no active Guess the User round in this channel.", ephemeral=True)
        return

    if user.id == round_info["answer_user_id"]:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO guess_user_scores (guild_id, user_id, score) VALUES (?, ?, 1) ON CONFLICT(guild_id, user_id) DO UPDATE SET score = score + 1",
                (interaction.guild.id, interaction.user.id),
            )
        await interaction.response.send_message("Correct!")
        await finish_guess_round(interaction.guild.id, interaction.channel.id, winner_id=interaction.user.id)
    else:
        await interaction.response.send_message("Nope. Keep guessing.", ephemeral=True)


@guessuser_group.command(name="leaderboard", description="Show Guess the User scores")
async def guessuser_leaderboard(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, score FROM guess_user_scores WHERE guild_id = ? ORDER BY score DESC LIMIT 10",
            (interaction.guild.id,),
        ).fetchall()
    if not rows:
        await interaction.response.send_message("No scores yet.")
        return
    lines = [f"{index}. <@{row['user_id']}> - {row['score']}" for index, row in enumerate(rows, start=1)]
    await interaction.response.send_message("\n".join(lines))


@guessuser_group.command(name="status", description="Show Guess the User status")
async def guessuser_status(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    module_config = get_module_config(loaded_config, "guess_user", interaction.guild.id)
    with get_db() as conn:
        clue_count = conn.execute("SELECT COUNT(*) AS count FROM guess_user_clues WHERE guild_id = ? AND active = 1", (interaction.guild.id,)).fetchone()["count"]
        optout_count = conn.execute("SELECT COUNT(*) AS count FROM guess_user_optouts WHERE guild_id = ?", (interaction.guild.id,)).fetchone()["count"]
    await interaction.response.send_message(
        f"Enabled: `{bool(module_config.get('enabled', True))}`\nActive clues: `{clue_count}`\nOpted-out users: `{optout_count}`\nIgnored channels: `{len(module_config.get('ignored_channel_ids', []))}`",
        ephemeral=True,
    )


@guessuser_group.command(name="scan", description="Scan one user for safe clues")
async def guessuser_scan(interaction: discord.Interaction, user: discord.Member) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    module_config = get_module_config(loaded_config, "guess_user", interaction.guild.id)
    if module_config.get("require_notice_before_scan", True) and not get_module_setting(interaction.guild.id, "guess_user", "notice_posted", False):
        await interaction.response.send_message("Post the privacy notice first with `/guessuser post-notice`, or disable `require_notice_before_scan`.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    inserted, scanned = await scan_guess_user(interaction.guild, user, loaded_config, incremental=True)
    await interaction.followup.send(f"Scanned {scanned} messages for {user.mention}; added {inserted} safe clues.", ephemeral=True)


@guessuser_group.command(name="rescan", description="Rescan one user from the full lookback window")
async def guessuser_rescan(interaction: discord.Interaction, user: discord.Member) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    inserted, scanned = await scan_guess_user(interaction.guild, user, loaded_config, incremental=False)
    await interaction.followup.send(f"Rescanned {scanned} messages for {user.mention}; added {inserted} safe clues.", ephemeral=True)


@guessuser_group.command(name="scan-server", description="Scan recent public messages for safe user clues")
async def guessuser_scan_server(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    module_config = get_module_config(loaded_config, "guess_user", interaction.guild.id)
    if module_config.get("require_notice_before_scan", True) and not get_module_setting(interaction.guild.id, "guess_user", "notice_posted", False):
        await interaction.response.send_message("Post the privacy notice first with `/guessuser post-notice`, or disable `require_notice_before_scan`.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    users_seen: dict[int, Any] = {}
    ignored_channel_ids = set(int(channel_id) for channel_id in module_config.get("ignored_channel_ids", []) or [])
    since = datetime.now().astimezone() - timedelta(days=int(module_config.get("lookback_days", 90)))
    readable_channels = 0
    skipped_channels = 0
    candidate_messages = 0

    for channel in interaction.guild.text_channels:
        if not is_public_server_text_channel(channel, ignored_channel_ids):
            skipped_channels += 1
            continue
        readable_channels += 1
        logging.info("Guess the User scan-server reading channel %s in guild %s", channel.id, interaction.guild.id)
        try:
            async for message in channel.history(after=since, limit=int(module_config.get("max_messages_per_channel", 1000)), oldest_first=False):
                if message.author.bot and not module_config.get("include_bots", False):
                    continue
                if not message.content:
                    continue
                if not guess_user_is_opted_out(interaction.guild.id, message.author.id):
                    users_seen[message.author.id] = message.author
                    candidate_messages += 1
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Skipping channel during Guess the User server scan: %s", channel.id)
        await asyncio.sleep(float(module_config.get("scan_delay_seconds", 0.5)))

    total_inserted = 0
    total_scanned = 0
    for user in users_seen.values():
        logging.info("Guess the User scan-server generating clues for user %s in guild %s", user.id, interaction.guild.id)
        inserted, scanned = await scan_guess_user(interaction.guild, user, loaded_config, incremental=True)
        total_inserted += inserted
        total_scanned += scanned

    await interaction.followup.send(
        f"Scanned {len(users_seen)} users and {total_scanned} user messages; added {total_inserted} safe clues.\n"
        f"Readable public channels: {readable_channels}. Skipped channels: {skipped_channels}. Candidate messages found: {candidate_messages}.",
        ephemeral=True,
    )


@guessuser_group.command(name="wipe-user", description="Admin wipe of a user's Guess the User data")
async def guessuser_wipe_user(interaction: discord.Interaction, user: discord.Member) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    delete_guess_user_data(interaction.guild.id, user.id)
    await interaction.response.send_message(f"Deleted Guess the User data for {user.mention}.", ephemeral=True)


@guessuser_group.command(name="ignore-channel", description="Exclude a channel from Guess the User scans")
async def guessuser_ignore_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    set_ignored_channel(interaction.guild.id, "guess_user", channel.id, True)
    await interaction.response.send_message(f"{channel.mention} will be ignored by Guess the User.", ephemeral=True)


@guessuser_group.command(name="unignore-channel", description="Include a channel in Guess the User scans again")
async def guessuser_unignore_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    set_ignored_channel(interaction.guild.id, "guess_user", channel.id, False)
    await interaction.response.send_message(f"{channel.mention} can be scanned by Guess the User again.", ephemeral=True)


@guessuser_group.command(name="post-notice", description="Post the Guess the User privacy notice")
async def guessuser_post_notice(interaction: discord.Interaction) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    notice = (
        "**Guess the User privacy notice**\n"
        "This server may use Guess the User, a party game that creates safe, vague clues from public server messages only. "
        "It does not use DMs, private channels, mod-only channels, ignored channels, bots, deleted messages, or sensitive topics. "
        "It stores generated clues and lightweight metadata, not raw message history. "
        "Use `/guessuser opt-out` anytime, and `/guessuser delete-my-data` to remove your game data."
    )
    await interaction.channel.send(notice)
    set_module_setting(interaction.guild.id, "guess_user", "notice_posted", True)
    await interaction.response.send_message("Posted the Guess the User privacy notice.", ephemeral=True)


@guessuser_group.command(name="remove-clue", description="Disable a Guess the User clue by ID")
async def guessuser_remove_clue(interaction: discord.Interaction, clue_id: int) -> None:
    loaded_config = await asyncio.to_thread(get_config)
    if not await require_admin_interaction(interaction, loaded_config):
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE guess_user_clues SET active = 0 WHERE guild_id = ? AND id = ?",
            (interaction.guild.id, clue_id),
        )
    await interaction.response.send_message(f"Disabled clue `{clue_id}`.", ephemeral=True)


discord_bot.tree.add_command(newspaper_group)
discord_bot.tree.add_command(mediator_group)
discord_bot.tree.add_command(guessuser_group)


@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@discord_bot.tree.command(name="model", description="View or switch the current model")
async def model_command(interaction: discord.Interaction, model: str) -> None:
    global curr_model

    if model == curr_model:
        output = f"Current model: `{curr_model}`"
    else:
        if user_is_admin := is_admin_user(interaction.user.id, config):
            curr_model = model
            output = f"Model switched to: `{model}`"
            logging.info(output)
        else:
            output = "You don't have permission to change the model."

    await interaction.response.send_message(output, ephemeral=interaction_is_private(interaction))


@model_command.autocomplete("model")
async def model_autocomplete(interaction: discord.Interaction, curr_str: str) -> list[Choice[str]]:
    global config

    if curr_str == "":
        config = await asyncio.to_thread(get_config)

    choices = [Choice(name=f"â—‰ {curr_model} (current)", value=curr_model)] if curr_str.lower() in curr_model.lower() else []
    choices += [Choice(name=f"â—‹ {model}", value=model) for model in config["models"] if model != curr_model and curr_str.lower() in model.lower()]

    return choices[:25]


@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@discord_bot.tree.command(name="channelmodel", description="Set this channel's model override")
async def channel_model_command(interaction: discord.Interaction, model: str) -> None:
    global config

    config = await asyncio.to_thread(get_config)

    if not is_admin_user(interaction.user.id, config):
        await interaction.response.send_message("You don't have permission to change channel models.", ephemeral=True)
        return

    if interaction.channel is None:
        await interaction.response.send_message("This command needs a channel.", ephemeral=True)
        return

    if model == "__default__":
        set_channel_override(interaction.channel, "model", None)
        output = f"This channel now uses the global model: `{curr_model}`"
    elif model in config["models"]:
        set_channel_override(interaction.channel, "model", model)
        output = f"This channel's model is now: `{model}`"
    else:
        output = "That model is not in `config.yaml`."

    await interaction.response.send_message(output, ephemeral=interaction_is_private(interaction))


@channel_model_command.autocomplete("model")
async def channel_model_autocomplete(interaction: discord.Interaction, curr_str: str) -> list[Choice[str]]:
    global config, channel_settings

    if curr_str == "":
        config = await asyncio.to_thread(get_config)
        channel_settings = await asyncio.to_thread(load_channel_settings)

    effective_model = get_effective_model(interaction.channel, config)
    choices = []

    if "default" in curr_str.lower() or curr_str == "":
        choices.append(Choice(name=f"Use global default ({curr_model})", value="__default__"))

    if curr_str.lower() in effective_model.lower():
        choices.append(Choice(name=f"Current: {effective_model}", value=effective_model))

    choices += [Choice(name=model, value=model) for model in config["models"] if model != effective_model and curr_str.lower() in model.lower()]
    return choices[:25]


@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@discord_bot.tree.command(name="persona", description="Set this channel's bot persona")
async def persona_command(interaction: discord.Interaction, persona: str) -> None:
    global config

    config = await asyncio.to_thread(get_config)

    if not is_admin_user(interaction.user.id, config):
        await interaction.response.send_message("You don't have permission to change personas.", ephemeral=True)
        return

    if interaction.channel is None:
        await interaction.response.send_message("This command needs a channel.", ephemeral=True)
        return

    personas = get_personas(config)

    if persona == "__default__":
        set_channel_override(interaction.channel, "persona", None)
        output = "This channel now uses the default persona."
    elif persona in personas:
        set_channel_override(interaction.channel, "persona", persona)
        output = f"This channel's persona is now: `{persona}`"
    else:
        output = "That persona is not in `config.yaml`."

    await interaction.response.send_message(output, ephemeral=interaction_is_private(interaction))


@persona_command.autocomplete("persona")
async def persona_autocomplete(interaction: discord.Interaction, curr_str: str) -> list[Choice[str]]:
    global config, channel_settings

    if curr_str == "":
        config = await asyncio.to_thread(get_config)
        channel_settings = await asyncio.to_thread(load_channel_settings)

    personas = get_personas(config)
    effective_persona = get_effective_persona(interaction.channel, config)
    choices = []

    if "default" in curr_str.lower() or curr_str == "":
        choices.append(Choice(name="Use default persona", value="__default__"))

    if curr_str.lower() in effective_persona.lower():
        choices.append(Choice(name=f"Current: {effective_persona}", value=effective_persona))

    choices += [Choice(name=name, value=name) for name in personas if name != effective_persona and curr_str.lower() in name.lower()]
    return choices[:25]


@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@discord.app_commands.describe(prompt="What you want the bot to answer", private="Only show the response to you")
@discord_bot.tree.command(name="ask", description="Ask the current model a question")
async def ask_command(interaction: discord.Interaction, prompt: str, private: bool = False) -> None:
    global config

    config = await asyncio.to_thread(get_config)

    if not user_has_permission_for_interaction(interaction, config):
        await interaction.response.send_message("You don't have permission to use this bot here.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=private)

    try:
        provider_slash_model = get_effective_model(interaction.channel, config)
        persona = get_effective_persona(interaction.channel, config)
        output = await generate_nonstream_response(prompt, interaction.user.id, config, provider_slash_model, persona)
        await send_interaction_chunks(interaction, output, private)
        logging.info(f"/ask completed (user ID: {interaction.user.id}, model: {provider_slash_model}, persona: {persona})")
    except Exception:
        logging.exception("Error while generating /ask response")
        await interaction.followup.send("Something went wrong while generating the response. Check the Render logs.", ephemeral=True)


@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@discord_bot.tree.command(name="retry", description="Regenerate the bot's most recent channel response")
async def retry_command(interaction: discord.Interaction) -> None:
    global config

    config = await asyncio.to_thread(get_config)

    if not user_has_permission_for_interaction(interaction, config):
        await interaction.response.send_message("You don't have permission to use this bot here.", ephemeral=True)
        return

    if interaction.channel is None:
        await interaction.response.send_message("This command needs a channel.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    retry_target = await find_retry_target(interaction.channel)
    if retry_target is None:
        await interaction.followup.send("I couldn't find a previous bot response to retry.", ephemeral=True)
        return

    await interaction.followup.send("Retrying the most recent response in this channel.", ephemeral=True)
    await send_streaming_reply(retry_target, config, log_label="/retry")


@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@discord.app_commands.describe(message_count="How many recent messages to summarize", private="Only show the summary to you")
@discord_bot.tree.command(name="summarize", description="Summarize recent channel messages")
async def summarize_command(interaction: discord.Interaction, message_count: int = 25, private: bool = False) -> None:
    global config

    config = await asyncio.to_thread(get_config)

    if not user_has_permission_for_interaction(interaction, config):
        await interaction.response.send_message("You don't have permission to use this bot here.", ephemeral=True)
        return

    if interaction.channel is None:
        await interaction.response.send_message("This command needs a channel.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=private)

    try:
        provider_slash_model = get_effective_model(interaction.channel, config)
        persona = get_effective_persona(interaction.channel, config)
        accept_images = any(x in provider_slash_model.lower() for x in VISION_MODEL_TAGS)
        limited_count = max(1, min(message_count, config.get("max_messages", 25)))
        recent_messages, user_warnings = await build_recent_channel_messages(interaction.channel, config, limited_count, accept_images)

        if not recent_messages:
            await interaction.followup.send("No recent messages found to summarize.", ephemeral=private)
            return

        api_messages = recent_messages[::-1]
        if system_prompt := get_system_prompt(config, persona):
            api_messages.insert(0, dict(role="system", content=system_prompt))
        api_messages.append(dict(role="user", content="Summarize the preceding Discord messages. Include key decisions, open questions, and action items if any. Keep it concise."))

        client_and_kwargs = build_openai_client_and_kwargs(config, provider_slash_model, api_messages, stream=False)
        response = await client_and_kwargs["client"].chat.completions.create(**client_and_kwargs["kwargs"])
        summary = response.choices[0].message.content or "*(empty summary)*"

        if user_warnings:
            summary = "\n".join(sorted(user_warnings)) + "\n\n" + summary

        await send_interaction_chunks(interaction, summary, private)
        logging.info(f"/summarize completed (user ID: {interaction.user.id}, messages: {len(recent_messages)}, model: {provider_slash_model})")
    except Exception:
        logging.exception("Error while generating /summarize response")
        await interaction.followup.send("Something went wrong while generating the summary. Check the Render logs.", ephemeral=True)


@discord_bot.event
async def on_ready() -> None:
    global newspaper_task

    if client_id := config.get("client_id"):
        logging.info(
            f"\n\nBOT SERVER INSTALL URL:\n"
            f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=412317191168&scope=bot%20applications.commands\n\n"
            f"USER INSTALL URL FOR /ask IN DMS AND GROUP DMS:\n"
            f"https://discord.com/oauth2/authorize?client_id={client_id}&scope=applications.commands&integration_type=1\n"
        )

    if newspaper_task is None or newspaper_task.done():
        newspaper_task = asyncio.create_task(newspaper_scheduler())

    await discord_bot.tree.sync()



def user_has_permission_for_message(new_msg: discord.Message, loaded_config: dict[str, Any]) -> bool:
    is_dm = new_msg.channel.type == discord.ChannelType.private
    role_ids = set(role.id for role in getattr(new_msg.author, "roles", ()))
    channel_ids = set(filter(None, (new_msg.channel.id, getattr(new_msg.channel, "parent_id", None), getattr(new_msg.channel, "category_id", None))))

    permissions = loaded_config["permissions"]
    user_is_admin = is_admin_user(new_msg.author.id, loaded_config)
    if user_is_admin:
        return True

    (allowed_user_ids, blocked_user_ids), (allowed_role_ids, blocked_role_ids), (allowed_channel_ids, blocked_channel_ids) = (
        (perm["allowed_ids"], perm["blocked_ids"]) for perm in (permissions["users"], permissions["roles"], permissions["channels"])
    )

    allow_all_users = not allowed_user_ids if is_dm else not allowed_user_ids and not allowed_role_ids
    is_good_user = user_is_admin or allow_all_users or new_msg.author.id in allowed_user_ids or any(id in allowed_role_ids for id in role_ids)
    is_bad_user = not is_good_user or new_msg.author.id in blocked_user_ids or any(id in blocked_role_ids for id in role_ids)

    allow_dms = loaded_config.get("allow_dms", True)
    allow_all_channels = not allowed_channel_ids
    is_good_channel = allow_dms if is_dm else allow_all_channels or any(id in allowed_channel_ids for id in channel_ids)
    is_bad_channel = not is_good_channel or any(id in blocked_channel_ids for id in channel_ids)

    return not is_bad_user and not is_bad_channel


@discord_bot.event
async def on_message(new_msg: discord.Message) -> None:
    is_dm = new_msg.channel.type == discord.ChannelType.private

    if (not is_dm and discord_bot.user not in new_msg.mentions) or new_msg.author.bot:
        return

    loaded_config = await asyncio.to_thread(get_config)

    if not user_has_permission_for_message(new_msg, loaded_config):
        return

    await send_streaming_reply(new_msg, loaded_config)


async def main() -> None:
    await discord_bot.start(config["bot_token"])


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
