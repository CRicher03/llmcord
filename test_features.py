"""Offline tests for commands, request lifecycle, and retry privacy."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import discord
from docx import Document
import httpx
from pypdf import PdfWriter

import llmcord as bot
import yaml


def interaction(user_id=123, channel_id=10, guild=True):
    return NS(user=NS(id=user_id, roles=[]), channel_id=channel_id,
              channel=NS(id=channel_id, parent_id=None, category_id=None),
              guild=NS(id=1) if guild else None, filesize_limit=10 * 1024 * 1024,
              response=NS(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False),
              followup=NS(send=AsyncMock()))


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = {
            "models": {"test/model": {}, "test/vision:vision": {}},
            "providers": {"test": {"base_url": "https://provider.example", "api_key": "test-key"}},
            "image_models": ["image/one", "image/two"],
            "permissions": {key: {"allowed_ids": [], "blocked_ids": []} for key in ("users", "roles", "channels")},
            "request_cooldown_seconds": 0,
            "max_concurrent_requests": 4,
            "system_prompt": "Original system prompt",
            "use_plain_responses": False,
            "response_buttons": False,
        }
        self.config["permissions"]["users"]["admin_ids"] = [999]
        for name, value in [("active_requests", {}), ("recent_requests", {}), ("request_cooldowns", {}),
                            ("channel_models", {}), ("msg_nodes", {}), ("curr_model", "test/model"),
                            ("curr_image_model", "image/one"), ("EDIT_DELAY_SECONDS", 0)]:
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(bot, "get_config", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=NS(choices=[NS(message=NS(content="answer"))])))), close=AsyncMock())
        patcher = patch.object(bot, "AsyncOpenAI", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, user_id=123, channel_id=10, kind="ask"):
        return bot.GenerationRequest(user_id, channel_id, kind, "test/model", "question")

    async def test_channel_defaults_inheritance_and_global_fallback(self):
        self.config["channel_models"] = {10: "test/vision:vision"}
        self.assertEqual(bot.get_effective_model(NS(id=11, parent_id=10), self.config), "test/vision:vision")
        bot.channel_models[11] = "test/model"
        self.assertEqual(bot.get_effective_model(NS(id=11, parent_id=10), self.config), "test/model")
        bot.channel_models[11] = "removed/model"
        self.assertEqual(bot.get_effective_model(NS(id=11, parent_id=10), self.config), "test/vision:vision")
        self.assertEqual(bot.get_effective_model(None, self.config), "test/model")
        with patch.object(bot, "curr_model", "removed/model"):
            self.assertEqual(bot.get_effective_model(None, self.config), "test/model")

    async def test_channel_command_permissions_view_set_reset(self):
        user = interaction()
        await bot.channel_model_command.callback(user, "test/vision:vision")
        self.assertEqual(bot.channel_models, {})
        self.assertIn("permission", user.response.send_message.call_args.args[0])
        admin = interaction(user_id=999)
        await bot.channel_model_command.callback(admin, "test/vision:vision")
        self.assertEqual(bot.channel_models, {10: "test/vision:vision"})
        await bot.channel_model_command.callback(admin, reset=True)
        self.assertEqual(bot.channel_models, {})
        await bot.channel_model_command.callback(admin)
        self.assertIn("test/model", admin.response.send_message.call_args.args[0])

    async def test_channel_command_rejects_invalid_combinations_and_dm(self):
        for model, reset, guild in [("missing", False, True), ("test/model", True, True), ("test/model", False, False)]:
            await bot.channel_model_command.callback(interaction(999, guild=guild), model=model, reset=reset)
            self.assertEqual(bot.channel_models, {})

    async def test_status_is_private_and_lists_limits_without_credentials(self):
        user = interaction()
        await bot.status_command.callback(user)
        call = user.response.send_message.call_args
        self.assertTrue(call.kwargs["ephemeral"])
        self.assertIn("test/model", call.args[0])
        self.assertIn("/stop", call.args[0])
        self.assertNotIn("test-key", call.args[0])
        self.assertNotIn("Admin:", call.args[0])
        self.assertLessEqual(len(call.args[0]), 2000)

    async def test_permissions_block_new_commands_before_generation(self):
        self.config["permissions"]["users"]["blocked_ids"] = [123]
        for command, kwargs in [(bot.ask_command, {"prompt": "question"}), (bot.image_command, {"prompt": "picture"}), (bot.retry_command, {}), (bot.status_command, {})]:
            user = interaction()
            await command.callback(user, **kwargs)
            self.assertIn("permission", user.response.send_message.call_args.args[0])
        self.client.chat.completions.create.assert_not_awaited()
        self.assertEqual(bot.recent_requests, {})

    async def test_cooldown_does_not_overwrite_retry_or_call_provider(self):
        self.config["request_cooldown_seconds"] = 30
        first = self.request()
        notify, operation = AsyncMock(), AsyncMock()
        self.assertTrue(await bot.run_generation(first, self.config, operation, notify))
        second = self.request()
        self.assertFalse(await bot.run_generation(second, self.config, operation, notify))
        operation.assert_awaited_once()
        self.assertIs(bot.recent_requests[(123, 10)], first)
        self.assertIn("wait", notify.call_args.args[0])
        self.assertEqual(bot.active_requests, {})

    async def test_global_limit_and_one_request_per_user_across_channels(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def long_operation():
            entered.set()
            await release.wait()
        running = asyncio.create_task(bot.run_generation(self.request(), self.config, long_operation, AsyncMock()))
        await entered.wait()
        try:
            operation, notify = AsyncMock(), AsyncMock()
            await bot.run_generation(self.request(channel_id=20), self.config, operation, notify)
            self.assertIn("already", notify.call_args.args[0])
            self.config["max_concurrent_requests"] = 1
            await bot.run_generation(self.request(user_id=456, kind="image"), self.config, operation, notify)
            self.assertIn("busy", notify.call_args.args[0])
            operation.assert_not_awaited()
        finally:
            release.set()
            await running
        self.assertEqual(bot.active_requests, {})

    async def test_stop_is_owner_and_channel_scoped_and_releases_slot(self):
        entered = asyncio.Event()
        async def operation():
            entered.set()
            await asyncio.Event().wait()
        notify = AsyncMock()
        task = asyncio.create_task(bot.run_generation(self.request(), self.config, operation, notify))
        await entered.wait()
        for wrong in [interaction(user_id=456), interaction(channel_id=20)]:
            await bot.stop_command.callback(wrong)
            self.assertFalse(task.cancelling())
        await bot.stop_command.callback(interaction())
        self.assertFalse(await task)
        self.assertEqual(bot.active_requests, {})
        self.assertIn("stopped", notify.call_args.args[0])
        self.assertIn((123, 10), bot.recent_requests)

    async def test_timeout_and_provider_failure_release_slots_and_keep_retry(self):
        self.config["request_timeout_seconds"] = 0.01
        notify = AsyncMock()
        with self.assertLogs(level="ERROR"):
            result = await bot.run_generation(self.request(), self.config, lambda: asyncio.sleep(1), notify)
        self.assertFalse(result)
        self.assertIn("timed out", notify.call_args.args[0])
        self.assertEqual(bot.active_requests, {})
        operation = AsyncMock(side_effect=RuntimeError("secret-key-in-error"))
        with self.assertLogs(level="ERROR"):
            await bot.run_generation(self.request(), self.config, operation, notify)
        self.assertNotIn("secret-key", notify.call_args.args[0])
        self.assertEqual(bot.active_requests, {})
        self.assertIn((123, 10), bot.recent_requests)

    async def test_private_ask_and_retry_preserve_snapshot_and_model_override(self):
        user = interaction()
        await bot.ask_command.callback(user, "original question", private=True)
        first_input = deepcopy(self.client.chat.completions.create.call_args.kwargs["messages"])
        self.config["system_prompt"] = "Changed system prompt"
        retry = interaction()
        await bot.retry_command.callback(retry, model="test/vision:vision")
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["messages"], first_input)
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["model"], "vision")
        self.assertEqual(bot.curr_model, "test/model")
        for call in [user, retry]:
            self.assertTrue(call.response.defer.call_args.kwargs["ephemeral"])
            self.assertTrue(all(sent.kwargs["ephemeral"] for sent in call.followup.send.call_args_list))
        self.assertNotIn("answer", str(first_input))

    async def test_retry_is_scoped_to_user_and_channel(self):
        bot.recent_requests[(123, 10)] = self.request()
        for other in [interaction(user_id=456), interaction(channel_id=20)]:
            await bot.retry_command.callback(other)
            self.assertIn("No recent request", other.response.send_message.call_args.args[0])
        self.client.chat.completions.create.assert_not_awaited()

    async def test_expired_and_evicted_requests_cannot_be_retried(self):
        request = self.request()
        request.created_at -= bot.RETRY_TTL_SECONDS + 1
        bot.recent_requests[(123, 10)] = request
        await bot.retry_command.callback(interaction())
        self.assertEqual(bot.recent_requests, {})
        for user_id in range(bot.MAX_RECENT_REQUESTS + 10):
            bot.recent_requests[(user_id, 10)] = self.request(user_id=user_id)
        bot.prune_recent_requests()
        self.assertEqual(len(bot.recent_requests), bot.MAX_RECENT_REQUESTS)
        self.assertNotIn((0, 10), bot.recent_requests)

    async def test_retry_rejects_removed_model_without_losing_original(self):
        request = self.request()
        request.model = "removed/model"
        bot.recent_requests[(123, 10)] = request
        user = interaction()
        await bot.retry_command.callback(user)
        self.assertIn("no longer configured", user.response.send_message.call_args.args[0])
        self.assertIs(bot.recent_requests[(123, 10)], request)

    async def test_failed_attachment_request_can_be_retried_with_vision_model(self):
        attachment = NS(filename="photo.png", content_type="image/png", size=3, url="https://cdn.example/photo")
        user = interaction()
        with self.assertLogs(level="ERROR"):
            await bot.ask_command.callback(user, "what is this?", private=True, attachment=attachment)
        self.client.chat.completions.create.assert_not_awaited()
        self.assertIn("vision", user.followup.send.call_args.args[0])
        with patch.object(bot, "download_context", AsyncMock(return_value=httpx.Response(200, content=b"png"))):
            await bot.retry_command.callback(interaction(), model="test/vision:vision")
        messages = self.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[-1]["content"][1]["type"], "image_url")

    async def test_text_attachment_downloaded_once_across_retry(self):
        attachment = NS(filename="notes.txt", content_type="text/plain", size=5, url="https://cdn.example/notes")
        download = AsyncMock(return_value=httpx.Response(200, text="file content"))
        with patch.object(bot, "download_context", download):
            await bot.ask_command.callback(interaction(), "summarize", attachment=attachment)
            first = deepcopy(self.client.chat.completions.create.call_args.kwargs["messages"])
            await bot.retry_command.callback(interaction())
        download.assert_awaited_once()
        self.assertIn("file content", str(first))
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["messages"], first)

    async def test_oversized_attachment_rejected_before_download_and_api(self):
        attachment = NS(filename="huge.pdf", content_type="application/pdf", size=30 * 1024 * 1024, url="https://cdn.example/huge")
        with patch.object(bot, "download_context", AsyncMock()) as download, self.assertLogs(level="ERROR"):
            user = interaction()
            await bot.ask_command.callback(user, "summarize", attachment=attachment)
            download.assert_not_awaited()
        self.client.chat.completions.create.assert_not_awaited()
        self.assertIn("smaller", user.followup.send.call_args.args[0])

    async def test_pdf_and_docx_ask_attachments(self):
        doc = Document()
        doc.add_paragraph("Document contents")
        doc_buffer = io.BytesIO()
        doc.save(doc_buffer)
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        for filename, kind, data in [("notes.docx", bot.DOCX_CONTENT_TYPE, doc_buffer.getvalue()), ("notes.pdf", "application/pdf", pdf_buffer.getvalue())]:
            attachment = NS(filename=filename, content_type=kind, size=len(data), url="https://cdn.example/file")
            with patch.object(bot, "download_context", AsyncMock(return_value=httpx.Response(200, content=data))):
                await bot.ask_command.callback(interaction(), "summarize", attachment=attachment)
            self.assertIn(filename, str(self.client.chat.completions.create.call_args.kwargs["messages"]))

    async def test_image_generation_and_retry_share_limits_and_use_override(self):
        generate = AsyncMock(return_value=(b"image", "image/png"))
        with patch.object(bot, "generate_openrouter_image", generate):
            first, retry = interaction(), interaction()
            await bot.image_command.callback(first, "a cat")
            await bot.retry_command.callback(retry, model="image/two")
        self.assertEqual(generate.await_args_list[0].args[:2], ("a cat", "image/one"))
        self.assertEqual(generate.await_args_list[1].args[:2], ("a cat", "image/two"))
        self.assertEqual(bot.curr_image_model, "image/one")
        self.assertIn("file", retry.followup.send.call_args.kwargs)
        self.assertFalse(retry.followup.send.call_args.kwargs["ephemeral"])

    async def test_image_retry_autocomplete_only_offers_image_models(self):
        bot.recent_requests[(123, 10)] = self.request(kind="image")
        choices = await bot.retry_model_autocomplete(interaction(), "")
        self.assertEqual([choice.value for choice in choices], ["image/one", "image/two"])

    async def test_registered_command_schemas_include_optional_attachment(self):
        tree = bot.discord_bot.tree
        names = {command.name for command in tree.get_commands()}
        self.assertTrue({"ask", "image", "model", "imagemodel", "channelmodel", "retry", "stop", "status"} <= names)
        schema = bot.ask_command.to_dict(tree)
        attachment = next(option for option in schema["options"] if option["name"] == "attachment")
        self.assertEqual(attachment["type"], discord.AppCommandOptionType.attachment.value)
        self.assertFalse(attachment["required"])
        for command in tree.get_commands():
            self.assertLessEqual(len(command.to_dict(tree)["description"]), 100)

    def streaming_message(self, rendered):
        @asynccontextmanager
        async def typing():
            yield

        class Message:
            channel = NS(id=10, type=discord.ChannelType.text, typing=typing)
            id = 1000
            author = NS(id=123, bot=False)
            content = "original prompt"
            attachments = []

            async def reply(self, *args, **kwargs):
                result = Message()
                result.id = len(rendered) + 2000
                result.data = deepcopy(kwargs.get("embed"))
                rendered.append(result)
                return result

            async def edit(self, **kwargs):
                self.data = deepcopy(kwargs["embed"])

        return Message()

    async def test_midstream_stop_releases_client_and_history_locks(self):
        rendered = []
        message = self.streaming_message(rendered)
        entered = asyncio.Event()
        async def stream():
            yield NS(choices=[NS(finish_reason=None, delta=NS(content="partial answer"))])
            entered.set()
            await asyncio.Event().wait()
        self.client.chat.completions.create.return_value = stream()
        request = self.request(kind="message")
        request.start_msg = message
        prepare = AsyncMock(return_value=([{"role": "user", "content": "original prompt"}], set()))
        notify = AsyncMock()
        with patch.object(bot, "build_reply_chain_messages", prepare):
            task = asyncio.create_task(bot.run_generation(request, self.config, lambda: bot.send_streaming_reply(message, self.config, request=request), notify))
            await entered.wait()
            self.assertTrue(bot.msg_nodes[rendered[0].id].lock.locked())
            await bot.stop_command.callback(interaction())
            self.assertFalse(await task)
        self.client.close.assert_awaited_once()
        self.assertFalse(bot.msg_nodes[rendered[0].id].lock.locked())
        self.assertEqual(rendered[0].data.description, "partial answer")
        self.assertEqual(rendered[0].data.footer.text, "Generation interrupted")
        self.assertEqual(rendered[0].data.color, bot.EMBED_COLOR_INCOMPLETE)
        self.assertEqual(bot.active_requests, {})
        self.assertNotIn("partial answer", str(request.messages))

    async def test_message_retry_uses_original_snapshot_and_reply_target(self):
        rendered = []
        message = self.streaming_message(rendered)
        request = self.request(kind="message")
        request.start_msg = message
        async def create(**kwargs):
            async def stream():
                yield NS(choices=[NS(finish_reason="stop", delta=NS(content="new answer"))])
            return stream()
        self.client.chat.completions.create.side_effect = create
        prepare = AsyncMock(return_value=([{"role": "user", "content": "original prompt"}, {"role": "assistant", "content": "earlier answer"}], set()))
        with patch.object(bot, "build_reply_chain_messages", prepare):
            await bot.run_generation(request, self.config, lambda: bot.send_streaming_reply(message, self.config, request=request), AsyncMock())
            first = deepcopy(self.client.chat.completions.create.call_args.kwargs["messages"])
            self.config["system_prompt"] = "new system prompt"
            retry = interaction()
            await bot.retry_command.callback(retry, model="test/vision:vision")
        prepare.assert_awaited_once()
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["messages"], first)
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["model"], "vision")
        self.assertEqual(len(rendered), 2)
        self.assertTrue(all(node.parent_msg is message for node in bot.msg_nodes.values()))
        self.assertTrue(retry.response.defer.call_args.kwargs["ephemeral"])

    async def test_mention_handler_registers_requests_and_reports_provider_errors(self):
        rendered = []
        message = self.streaming_message(rendered)
        user = NS(id=42)
        message.mentions = [user]
        message.reply = AsyncMock()
        async def generate(*args, **kwargs):
            self.assertIn(message.author.id, bot.active_requests)
            raise TimeoutError()
        with patch.object(bot, "discord_bot", NS(user=user)), patch.object(bot, "send_streaming_reply", side_effect=generate), self.assertLogs(level="ERROR"):
            await bot.on_message(message)
        self.assertIn("timed out", message.reply.call_args.args[0])
        self.assertIn((123, 10), bot.recent_requests)
        self.assertEqual(bot.active_requests, {})


class ErrorTests(unittest.TestCase):
    def test_config_accepts_numeric_channel_ids_and_rejects_invalid_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            config = {"channel_models": {123: "test/model"}}
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            self.assertEqual(bot.get_config(str(path))["channel_models"], {123: "test/model"})
            for key, value in [("max_concurrent_requests", 0), ("max_concurrent_requests", 1.5), ("request_cooldown_seconds", -1), ("request_timeout_seconds", True)]:
                path.write_text(yaml.safe_dump({key: value}), encoding="utf-8")
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    bot.get_config(str(path))

    def test_error_messages_are_specific_and_do_not_expose_response_body(self):
        for status, expected in [(429, "rate-limit"), (401, "credentials"), (402, "credits"),
                                 (404, "unavailable"), (413, "too large"), (400, "rejected"), (503, "temporarily")]:
            error = httpx.HTTPStatusError("secret response body", request=httpx.Request("GET", "https://example.com"), response=httpx.Response(status))
            text = bot.friendly_error(error)
            self.assertIn(expected, text)
            self.assertNotIn("secret", text)
        self.assertIn("timed out", bot.friendly_error(TimeoutError()))
        self.assertIn("reach", bot.friendly_error(httpx.ConnectError("secret")))

    def test_model_conversion_does_not_mutate_retry_snapshot(self):
        snapshot = [{"role": "user", "content": [{"type": "text", "text": "question"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,aA=="}}]}]
        self.assertEqual(bot.messages_for_model(snapshot, "test/model"), [{"role": "user", "content": "question"}])
        self.assertEqual(len(snapshot[0]["content"]), 2)


if __name__ == "__main__":
    unittest.main()
