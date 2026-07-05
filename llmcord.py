import asyncio
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime
import io
import ipaddress
import logging
import os
import json
import re
from typing import Any, Literal, Optional
from urllib.parse import urlparse

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
URL_RE = re.compile(r"https?://[^\s<>()\]\}]+")

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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
curr_model = next(iter(config["models"]))
channel_settings = load_channel_settings()

msg_nodes = {}
last_task_time = 0

intents = discord.Intents.default()
intents.message_content = True
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
    if client_id := config.get("client_id"):
        logging.info(
            f"\n\nBOT SERVER INSTALL URL:\n"
            f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=412317191168&scope=bot%20applications.commands\n\n"
            f"USER INSTALL URL FOR /ask IN DMS AND GROUP DMS:\n"
            f"https://discord.com/oauth2/authorize?client_id={client_id}&scope=applications.commands&integration_type=1\n"
        )

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
