<h1 align="center">
  llmcord
</h1>

<h3 align="center"><i>
  Talk to LLMs with your friends!
</i></h3>

<p align="center">
  <img src="https://github.com/user-attachments/assets/7791cc6b-6755-484f-a9e3-0707765b081f" alt="">
</p>

llmcord transforms Discord into a collaborative LLM frontend. It works with practically any LLM, remote or locally hosted.

## Features

### Reply-based conversations:
Just @ the bot to start a conversation and reply to continue. Build conversations with reply chains!

The reply chain is the conversation history, stored entirely in Discord. No database required.

You can:
- Branch conversations endlessly
- Continue other people's conversations
- @ the bot while replying to ANY message to include it in the conversation

Additionally:
- When DMing the bot, conversations continue automatically (no reply required). To start a fresh conversation, just @ the bot. You can still reply to continue from anywhere.
- You can branch conversations into [threads](https://support.discord.com/hc/en-us/articles/4403205878423-Threads-FAQ). Just create a thread from any message and @ the bot inside to continue.
- Back-to-back messages from the same user are automatically chained together. Just reply to the latest one and the bot will see all of them.

---

### Model switching with `/model`:

Administrators can use `/model` to switch the model used by both app-command and mention/reply conversations. Model selections can optionally survive restarts (enabled in the supplied configuration).

Administrators can similarly use `/imagemodel` to switch the default used by `/image`. An optional model supplied directly to `/image` overrides it for that request only.

Use `/channelmodel model:...` to set a text model for the current server channel or thread. Threads inherit their parent channel's model unless they have their own default. `/channelmodel` shows the effective model, and `/channelmodel reset:true` removes the command override. These commands are admin-only; `/model` still changes the global fallback.

`persist_model_selections: true` saves `/model`, `/imagemodel`, and channel command overrides in `model_state_file` (default: `data/model-selections.json`). Writes replace the file atomically; only model names and channel IDs are saved, never prompts or API keys. Removed models and unreadable state fall back to configured defaults. A save failure leaves the selection active for this session and is reported to the administrator. Set the option to `false` for session-only selections. The supplied Docker Compose file mounts a named volume at `/app/data`; other hosts need writable persistent storage at the configured path. State is restored at startup, so changing the persistence option or state path takes full effect on the next restart.

For defaults that survive restart, add channel IDs and existing model keys to `channel_models` in `config.yaml`, for example:

```yaml
channel_models:
  "123456789012345678": "openrouter/openrouter/auto"
```

Selection order is the current channel's command override, its configured default, then the parent channel's override/default, then the global model. References to models removed from the configuration are ignored.

### Channel prompts

Admins can use `/channelprompt prompt:...` in a server channel or thread to replace the global system prompt there. For a schooling channel, for example:

```text
/channelprompt prompt:You are a patient educational tutor. Keep language school-appropriate. Explain concepts clearly, adapt to the student's level, and use examples and practice questions. Guide the student through reasoning and check their understanding. Be honest about uncertainty. User messages are prefixed with Discord IDs as <@ID>.
```

`/channelprompt` privately reports which setting is active without displaying prompt text. `/channelprompt reset:true` removes the command override. Selection order is the current channel's command override, its configured `channel_prompts` entry, then the parent's override/configuration, then the global `system_prompt`. Threads inherit their parent's prompt unless overridden. `{date}` and `{time}` work in channel prompts too. New text requests (chat, `/ask`, comparisons, battles, and debates) use the selected prompt. Retries and answer follow-ups preserve the original prepared conversation and prompt; existing conversation messages are not cleared.

Command overrides survive restarts by default in `prompt_state_file` (`data/channel-prompts.json`), separate from model selections. This file contains the prompt text; keep it on writable persistent storage. Set `persist_channel_prompts: false` for session-only overrides. Save failures are reported and leave the override active for the session. Restart after changing the persistence settings or updating the bot code to register the new command.

You can also set permanent defaults in `config.yaml` (an empty string explicitly disables the system prompt for that channel):

```yaml
channel_prompts:
  "123456789012345678": |
    You are a patient educational tutor. Keep language school-appropriate.
    Explain concepts clearly, give examples, and check understanding.
```

### Everyday commands

| Command | What it does |
| --- | --- |
| `/ask prompt:...` | Ask a question. Add `attachment:` for one image, text file, PDF, or DOCX, and `private:true` for a response visible only to you. Images require a model configured for vision. |
| `/image prompt:...` | Generate an image, optionally choosing a one-off `model:`. |
| `/stop` | Stop your own active generation in this channel, including chat, `/ask`, `/image`, and retries. Already posted partial replies remain and are marked interrupted when using embeds. |
| `/retry` | Retry your latest accepted request in this channel. Optional `model:` selects another configured text or image model for this retry only. |
| `/status` | Privately show the effective models, conversation/file limits, active request count, and available commands. |
| `/help` | Privately show a compact guide to reply chains, attachments, model selection, and private responses, with command examples. |
| `/battle prompt:...` | Randomly select two configured text models, show answers A/B, and reveal the model names after the requester votes A, B, or Tie. Supports `private:true`. |
| `/debate topic:... model_a:... model_b:...` | Run one round between two models. Use **Steer next round** to enter a direction and continue. Supports `private:true`. |


Generations show a single temporary progress message: **Working...**. On completion, controls move to the last answer or file message and the progress message is removed, where Discord permits these edits. A muted footer shows the model (without its internal provider prefix) and completion time separated by a single space, without divider lines, including embed replies.

Text answers have no action buttons. Reply to an answer to continue the conversation, or use `/retry model:…` to rerun its prompt with another model.

Battles show anonymous A/B labels in answers and a blind-battle footer until voting; generated answer text itself may contain model self-identification. A single requester vote reveals the mapping in the same public/private scope as the battle. Votes are session-only, with no leaderboard. Both contenders get identical input and neither sees the other's answer. In debates, the second model sees the first model's argument. Each submitted steering direction runs exactly one more two-model round; nothing continues automatically. Retrying repeats the selected battle or debate round with its original input. An incomplete battle cannot be voted on; an incomplete debate must be retried before continuing.

Only the original requester can use controls, and current bot permissions are checked. Controls expire after 15 minutes or a bot restart; expiry disables them without replacing the answer. `/stop` and `/retry` remain available as slash commands. Set `response_buttons: false` to disable ordinary answer progress; model/time footers remain. Battles and debates always show the controls needed to vote or continue.

Inline responses balance standard backtick and tilde code fences across message boundaries, reopening the language block in continuation messages. Temporary closing fences also keep streamed code readable. Downloaded files and cached model context preserve the original text without these display-only markers. Edits to user message text or attachments invalidate cached context, including edits to messages outside Discord's own message cache. Future conversations fetch the corrected message; an in-progress request or retry keeps its already prepared input snapshot.

Answers longer than `long_answer_threshold` (default: 6,000 characters) are delivered as a short preview plus a complete Markdown attachment for app commands and plain chat replies. Embed replies keep streaming and receive a Markdown attachment on completion. Files preserve code blocks and Unicode. If the file exceeds Discord's upload limit, answers remain split into messages; set the threshold to `0` to keep answers inline. Private responses and their files stay private. Image responses contain their downloadable image.

Retries preserve the original prepared conversation, including attachment content, without adding the previous generated answer. A mention/reply retry creates a new branch from the original message. Private `/ask` retries stay private. If a request failed before its input was prepared, retry attempts preparation again. Retry records are kept in memory, with one latest request per user/channel, up to 50 records total, and are available for up to 30 minutes; restarting clears them. Image retries generate a new image from the same prompt.

One generation may run per user across all channels. The default shared limit is four simultaneous generations, with a three-second per-user cooldown between starts and a ten-minute total request timeout. Busy requests are rejected with an explanation instead of queued. These limits also apply to administrators and retries; `/stop` and `/status` remain usable while busy. Change `max_concurrent_requests`, `request_cooldown_seconds` (set to `0` to disable), and `request_timeout_seconds` in the configuration. Stopping closes the bot's active request; provider-side processing or billing may already have occurred.

Failures now explain timeouts, rate limits, unavailable models/files, oversized inputs, and provider access or credit problems. Detailed exceptions stay in the bot logs. Plain chat replies also report skipped-input warnings.

llmcord supports remote models from:
- [OpenRouter](https://openrouter.ai/models)
- [OpenAI](https://platform.openai.com/docs/models)
- [xAI](https://docs.x.ai/docs/models)
- [Google](https://ai.google.dev/gemini-api/docs/models)

Or run local models with:
- [LM Studio](https://lmstudio.ai)
- [Ollama](https://ollama.com)
- [vLLM](https://github.com/vllm-project/vllm)

...Or use any other OpenAI /v1/chat/completions compatible API server.

---

### And more:
- Supports user-installed `/ask`, `/image`, `/retry`, `/stop`, and `/status` app commands in DMs and group DMs
- Image generation through OpenRouter with a curated model selector
- Admin model switching with `/model` and `/imagemodel`
- Supports image attachments when using a vision model (like gpt-5, grok-4, claude-4, etc.)
- Supports text, PDF, DOCX, and URL ingestion
- Customizable personality (aka system prompt)
- Distinguishes users via their Discord IDs
- Streamed responses (turns green when complete, automatically splits into separate messages when too long)
- Hot reloading config (you can change settings without restarting the bot)
- Displays helpful warnings when appropriate (like "⚠️ Only using last 25 messages" when the customizable message limit is exceeded)
- Caches message data in a size-managed (no memory leaks) and mutex-protected (no race conditions) global dictionary to maximize efficiency and minimize Discord API calls
- Fully asynchronous
- Single-file bot

## Instructions

1. Clone the repo:
   ```bash
   git clone https://github.com/jakobdylanc/llmcord
   cd llmcord
   ```

2. Set up `config.yaml`:

> Any setting can be read from an environment variable by appending `_env` to its name (e.g. `bot_token_env: DISCORD_BOT_TOKEN`).

Permission ID settings accept a single ID, a JSON list, or comma-separated IDs in environment variables. Empty or unset values become empty lists; an empty allowlist allows everyone in that category. Invalid IDs raise a configuration error.

URL and attachment downloads are limited to four concurrent requests and `max_download_bytes` per file (default: 20 MiB). `url_fetch_timeout` is a total time limit per download, including DNS and redirects (default: 10 seconds). PDF extraction accepts at most `max_pdf_pages` (default: 100), and DOCX files may expand to at most `max_docx_expanded_bytes` (default: 50 MiB). Adjust these settings for larger documents. Oversized or unreadable files are skipped; embed responses display the existing attachment/URL warnings.

Context downloads only access public HTTP(S) destinations, validating DNS and each redirect. They bypass environment proxies and request uncompressed responses; servers that insist on HTTP compression are skipped. These restrictions apply to context ingestion, so local LLM provider endpoints still work.

### Discord settings:

| Setting | Description |
| --- | --- |
| **bot_token** | Create a new Discord bot at [discord.com/developers/applications](https://discord.com/developers/applications) and generate a token under the "Bot" tab. Also enable "MESSAGE CONTENT INTENT". |
| **client_id** | Found under the "OAuth2" tab of the Discord bot you just made. |
| **status_message** | Set a custom message that displays on the bot's Discord profile.<br /><br />**Max 128 characters.** |
| **max_text** | The maximum amount of text allowed in a single message, including text from file attachments.<br /><br />Default: `100,000` |
| **max_images** | The maximum number of image attachments allowed in a single message.<br /><br />Default: `5`<br /><br />**Only applicable when using a vision model.** |
| **max_messages** | The maximum number of messages allowed in a reply chain. When exceeded, the oldest messages are dropped.<br /><br />Default: `25` |
| **use_plain_responses** | When set to `true` the bot will use plaintext responses instead of embeds. Plaintext responses have a shorter character limit so the bot's messages may split more often.<br /><br />Default: `false`<br /><br />**Disables visible streaming; input warnings are sent separately.** |
| **allow_dms** | Set to `false` to disable direct message access.<br /><br />Default: `true` |
| **permissions** | Configure access permissions for `users`, `roles` and `channels`, each with a list of `allowed_ids` and `blocked_ids`.<br /><br />Control which `users` are admins with `admin_ids`. Admins can use `/model` and `/imagemodel`, and can DM the bot even if `allow_dms` is `false`.<br /><br />**Leave `allowed_ids` empty to allow ALL in that category.**<br /><br />**Role and channel permissions do not affect DMs.**<br /><br />**You can use [category](https://support.discord.com/hc/en-us/articles/115001580171-Channel-Categories-101) IDs to control channel permissions in groups.** |

### LLM settings:

| Setting | Description |
| --- | --- |
| **providers** | Add the LLM providers you want to use, each with a `base_url` and optional `api_key` entry. Popular providers (`openrouter`, `openai`, `ollama`, etc.) are already included.<br /><br />**Only supports OpenAI /v1/chat/completions compatible APIs.**<br /><br />**Some providers may need `extra_headers` / `extra_query` / `extra_body` entries for extra HTTP data. See the included `azure-openai` provider for an example.** |
| **models** | Add models in `<provider>/<model>: <parameters>` format (examples are included). The bot starts with the first model in the list; administrators can switch among configured models with `/model`.<br /><br />**Refer to the provider's documentation for supported parameters.**<br /><br />**Some vision models may need `:vision` added to the end of their name to enable image support.** |
| **image_models** | OpenRouter image-generation model IDs offered by `/image` and `/imagemodel`. The first entry is the startup default when no valid saved selection exists. `/imagemodel` changes the default; `/image` can select a one-off override. |
| **system_prompt** | Write anything you want to customize the bot's behavior!<br /><br />**Leave blank for no system prompt.**<br /><br />**You can use the `{date}` and `{time}` tags in your system prompt to insert the current date and time, based on your host computer's time zone.**<br /><br />**It is recommended to include something like `"User messages are prefixed with their Discord ID as <@ID>. Use this format to mention users."` in your system prompt to help the bot understand the user message format.** |

3. Run the bot:

   **No Docker:**
   ```bash
   python -m pip install -U -r requirements.txt
   python llmcord.py
   ```

   **With Docker:**
   ```bash
   docker compose up
   ```

## Notes

- Run offline regression checks with `python -m unittest -v test_llmcord test_features test_response_tools test_polish` after installing the requirements. Tests do not log into Discord or call model providers.
- Restart the bot after updating the code so its startup command sync registers new slash commands. Retry history resets on restart; command model overrides survive when persistence is enabled and storage is retained. `channel_models` configuration always persists.

- If you're having issues, try my suggestions [here](https://github.com/jakobdylanc/llmcord/issues/19)

- PRs are welcome :)

## Star History

<a href="https://star-history.com/#jakobdylanc/llmcord&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=jakobdylanc/llmcord&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=jakobdylanc/llmcord&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=jakobdylanc/llmcord&type=Date" />
  </picture>
</a>
