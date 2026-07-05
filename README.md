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
![image](https://github.com/user-attachments/assets/568e2f5c-bf32-4b77-ab57-198d9120f3d2)

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
- Supports image attachments when using a vision model (like gpt-5, grok-4, claude-4, etc.)
- Supports text, PDF, DOCX, and URL ingestion
- Customizable personality (aka system prompt)
- Per-channel model and persona overrides with `/channelmodel` and `/persona`
- Regenerate or summarize conversations with `/retry` and `/summarize`
- Daily AI-generated server newspaper with `/newspaper`
- Neutral low-stakes disagreement mediation with `/mediate`
- Reputation titles with `/titles`
- General-audience party games with `/party`
- Attachment/file Q&A with `/file`
- Distinguishes users via their Discord IDs
- Streamed responses (turns green when complete, automatically splits into separate messages when too long)
- Hot reloading config (you can change settings without restarting the bot)
- Displays helpful warnings when appropriate (like "⚠️ Only using last 25 messages" when the customizable message limit is exceeded)
- Caches message data in a size-managed (no memory leaks) and mutex-protected (no race conditions) global dictionary to maximize efficiency and minimize Discord API calls
- Fully asynchronous
- Single-file bot with SQLite-backed social modules

## Instructions

1. Clone the repo:
   ```bash
   git clone https://github.com/jakobdylanc/llmcord
   cd llmcord
   ```

2. Set up `config.yaml`:

> Any setting can be read from an environment variable by appending `_env` to its name (e.g. `bot_token_env: DISCORD_BOT_TOKEN`).

### Discord settings:

| Setting | Description |
| --- | --- |
| **bot_token** | Create a new Discord bot at [discord.com/developers/applications](https://discord.com/developers/applications) and generate a token under the "Bot" tab. Also enable "MESSAGE CONTENT INTENT". |
| **client_id** | Found under the "OAuth2" tab of the Discord bot you just made. |
| **status_message** | Set a custom message that displays on the bot's Discord profile.<br /><br />**Max 128 characters.** |
| **max_text** | The maximum amount of text allowed in a single message, including text from file attachments.<br /><br />Default: `100,000` |
| **max_images** | The maximum number of image attachments allowed in a single message.<br /><br />Default: `5`<br /><br />**Only applicable when using a vision model.** |
| **max_messages** | The maximum number of messages allowed in a reply chain. When exceeded, the oldest messages are dropped.<br /><br />Default: `25` |
| **use_plain_responses** | When set to `true` the bot will use plaintext responses instead of embeds. Plaintext responses have a shorter character limit so the bot's messages may split more often.<br /><br />Default: `false`<br /><br />**Also disables streamed responses and warning messages.** |
| **allow_dms** | Set to `false` to disable direct message access.<br /><br />Default: `true` |
| **permissions** | Configure access permissions for `users`, `roles` and `channels`, each with a list of `allowed_ids` and `blocked_ids`.<br /><br />Control which `users` are admins with `admin_ids`. Admins can change the model with `/model` and DM the bot even if `allow_dms` is `false`.<br /><br />**Leave `allowed_ids` empty to allow ALL in that category.**<br /><br />**Role and channel permissions do not affect DMs.**<br /><br />**You can use [category](https://support.discord.com/hc/en-us/articles/115001580171-Channel-Categories-101) IDs to control channel permissions in groups.** |

### LLM settings:

| Setting | Description |
| --- | --- |
| **providers** | Add the LLM providers you want to use, each with a `base_url` and optional `api_key` entry. Popular providers (`openrouter`, `openai`, `ollama`, etc.) are already included.<br /><br />**Only supports OpenAI /v1/chat/completions compatible APIs.**<br /><br />**Some providers may need `extra_headers` / `extra_query` / `extra_body` entries for extra HTTP data. See the included `azure-openai` provider for an example.** |
| **models** | Add the models you want to use in `<provider>/<model>: <parameters>` format (examples are included). When you run `/model` these models will show up as autocomplete suggestions.<br /><br />**Refer to each provider's documentation for supported parameters.**<br /><br />**The first model in your `models` list will be the default model at startup.**<br /><br />**Some vision models may need `:vision` added to the end of their name to enable image support.** |
| **system_prompt** | Write anything you want to customize the bot's behavior!<br /><br />**Leave blank for no system prompt.**<br /><br />**You can use the `{date}` and `{time}` tags in your system prompt to insert the current date and time, based on your host computer's time zone.**<br /><br />**It is recommended to include something like `"User messages are prefixed with their Discord ID as <@ID>. Use this format to mention users."` in your system prompt to help the bot understand the user message format.** |

### Social modules:

These modules are configured under `modules:` in `config.yaml`. Per-server runtime settings are stored in `llmcord.sqlite3`.

#### Daily Server Newspaper

Generates a fun, general-audience summary of the last 24 hours of readable public server activity. It ignores private/mod-only channels, channels the bot cannot read, and configured ignored channels.

Commands:
- `/newspaper set-channel #channel`
- `/newspaper enable`
- `/newspaper disable`
- `/newspaper generate`
- `/newspaper ignore-channel #channel`
- `/newspaper unignore-channel #channel`
- `/newspaper status`

By default it posts at `00:00` in `America/New_York`. Change `modules.server_newspaper.timezone` or `post_time` in `config.yaml`.

#### AI Mediator

Use `/mediate topic:<text> side_a:<optional> side_b:<optional>` for neutral summaries of low-stakes disagreements, planning conflicts, and decision-making. It adds a disclaimer for serious topics and refuses to mediate threats, self-harm, abuse, harassment, violence, or emergencies.

Admin controls:
- `/mediator enable`
- `/mediator disable`
- `/mediator status`

#### Reputation Titles

Tracks lightweight, non-sensitive public server activity and awards fun titles such as `Night Owl`, `Early Bird`, `Link Supplier`, `Helpful Human`, `Reaction Magnet`, `Voice Chat Regular`, `Game Goblin`, and `Server Regular`.

It ignores DMs, private/mod-only channels, ignored channels, bots, and users who opt out. It stores counters and earned titles only, not raw message contents.

Player commands:
- `/titles profile`
- `/titles list`
- `/titles equip title:<title>`
- `/titles leaderboard`
- `/titles opt-out`
- `/titles opt-in`

Admin commands:
- `/titles grant user:@user title:<title>`
- `/titles remove user:@user title:<title>`
- `/titles status`
- `/titles enable`
- `/titles disable`

#### Party Game Pack

Adds lightweight, general-audience games with per-server scores and history in SQLite. AI prompts are safe-filtered and fall back to static prompts if generation fails.

Commands:
- `/party would-you-rather`
- `/party never-have-i-ever`
- `/party this-or-that`
- `/party trivia category:<category> difficulty:<difficulty>`
- `/party two-truths-start statement_1:<text> statement_2:<text> statement_3:<text> lie_number:<1-3>`
- `/party two-truths-guess lie_number:<1-3>`
- `/party wordchain-start first_word:<word>`
- `/party wordchain word:<word>`
- `/party guess-start`
- `/party guess-answer guess:<text>`
- `/party leaderboard`
- `/party stats`
- `/party stop`
- `/party status`
- `/party enable game:<game>`
- `/party disable game:<game>`

#### Attachment Brain

Lets users ask questions about uploaded files without storing file contents long-term. Supports common text/code/log files, PDFs, DOCX, JSON/CSV/Markdown, and images when a vision-capable model is configured.

Commands:
- `/file summarize attachment:<file>`
- `/file ask attachment:<file> question:<text>`
- `/file extract-text attachment:<file>`
- `/file debug-log attachment:<file>`
- `/file explain-code attachment:<file>`
- `/file convert-json attachment:<file>`
- `/file status`

Configure limits with `modules.attachment_brain.max_file_size_mb`, `max_extracted_chars`, `allowed_extensions`, `model`, and `vision_model`.

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
