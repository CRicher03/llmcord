"""Offline checks for response UI, formatting, persistence, edits, and help."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import discord
import llmcord as bot
import test_features as fixtures


class FormattingTests(unittest.TestCase):
    def test_code_fences_and_exact_raw_text(self):
        for marker in ("```", "~~~~", "````"):
            for code in ("print('hello')\n" * 1000, "x" * 10000, "日本語\n" * 1000):
                text = "Intro\n" + marker + "python\n" + code + "\n" + marker + "\nConclusion"
                parts = bot.markdown_parts(text, 1700)
                self.assertEqual("".join(raw for raw, display in parts), text)
                for raw, display in parts:
                    self.assertLessEqual(len(display), 1700)
                    fences = re.findall(r"(?m)^" + re.escape(marker) + r"(?:python)?\s*$", display)
                    self.assertEqual(len(fences) % 2, 0, repr(display[-100:]))

    def test_unclosed_fence_is_closed_for_display_only(self):
        text = "```js\n" + "const x = 1;\n" * 300
        parts = bot.markdown_parts(text, 1700)
        self.assertEqual("".join(raw for raw, _ in parts), text)
        self.assertTrue(all(display.endswith("```") for _, display in parts))
        self.assertTrue(all(display.startswith("```js\n") for _, display in parts))

    def test_plain_text_empty_and_fence_at_boundary(self):
        self.assertEqual(bot.markdown_parts("", 1700), [])
        for offset in range(1430, 1460):
            text = "x" * offset + "\n```python\n" + "z" * 6000 + "\n```"
            parts = bot.markdown_parts(text, 1700)
            self.assertEqual("".join(raw for raw, _ in parts), text)
            self.assertTrue(all(len(display) <= 1700 for _, display in parts))


class PolishTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FeatureTests.asyncSetUp
    request = fixtures.FeatureTests.request
    streaming_message = fixtures.FeatureTests.streaming_message

    async def test_saved_models_survive_restore_without_storing_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            self.config.update(persist_model_selections=True, model_state_file=str(Path(directory) / "state.json"))
            admin = fixtures.interaction(999)
            await bot.model_command.callback(admin, "test/vision:vision")
            await bot.image_model_command.callback(admin, "image/two")
            await bot.channel_model_command.callback(admin, "test/model")
            state = json.loads(Path(self.config["model_state_file"]).read_text())
            self.assertEqual(set(state), {"model", "image_model", "channel_models"})
            bot.curr_model, bot.curr_image_model = "test/model", "image/one"
            bot.channel_models.clear()
            bot.restore_model_selections(self.config)
            self.assertEqual(bot.curr_model, "test/vision:vision")
            self.assertEqual(bot.curr_image_model, "image/two")
            self.assertEqual(bot.channel_models, {10: "test/model"})
            await bot.channel_model_command.callback(admin, reset=True)
            self.assertEqual(json.loads(Path(self.config["model_state_file"]).read_text())["channel_models"], {})

    async def test_disabled_removed_corrupt_and_failed_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            config = self.config | {"model_state_file": str(path)}
            bot.save_model_selections(config)
            self.assertFalse(path.exists())
            config["persist_model_selections"] = True
            path.write_text(json.dumps({"model": "removed/model", "image_model": "removed/image", "channel_models": {"10": "removed/model"}}))
            bot.restore_model_selections(config)
            self.assertEqual(bot.curr_model, "test/model")
            self.assertEqual(bot.channel_models, {})
            path.write_text("{bad json")
            with self.assertLogs(level="ERROR"):
                bot.restore_model_selections(config)
            self.assertEqual(bot.curr_model, "test/model")
            with patch.object(bot.os, "replace", side_effect=PermissionError()), self.assertLogs(level="ERROR"):
                self.assertIn("saving failed", bot.save_model_selections(config))
            self.assertEqual(path.read_text(), "{bad json")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    async def test_controls_move_to_private_answer_with_footer_and_progress_is_deleted(self):
        self.config["response_buttons"] = True
        user = fixtures.interaction()
        progress, answer = NS(edit=AsyncMock(), delete=AsyncMock()), NS(edit=AsyncMock())
        user.edit_original_response.return_value = progress
        user.followup.send.return_value = answer
        await bot.ask_command.callback(user, "question", private=True)
        progress.delete.assert_awaited_once()
        args = answer.edit.call_args.kwargs
        self.assertIn("answer", args["content"])
        self.assertIn("test/model", args["content"])
        self.assertRegex(args["content"], r"· \d+\.\ds$")
        self.assertLessEqual(len(args["content"]), 2000)
        view = args["view"]
        try:
            self.assertIs(view.message, answer)
            self.assertFalse(view.download_response.disabled)
            stages = [call.kwargs.get("content") for call in progress.edit.call_args_list]
            self.assertIn("Working.", stages)
            await view.on_timeout()
            self.assertNotIn("content", answer.edit.call_args.kwargs)
        finally:
            view.stop()
        self.assertTrue(all(call.kwargs["ephemeral"] for call in user.followup.send.call_args_list))

    async def test_attachment_and_comparison_progress_stages(self):
        request = self.request()
        request.attachment = NS(filename="notes.txt", content_type="text/plain", size=3, url="https://cdn.example/file")
        progress = NS(edit=AsyncMock())
        request.controls = NS(message=progress)
        with patch.object(bot, "download_context", AsyncMock(return_value=bot.httpx.Response(200, text="notes"))):
            await bot.prepare_ask_request(request, self.config)
        self.assertIn("Reading attachment…", [call.kwargs["content"] for call in progress.edit.call_args_list])
        self.config["response_buttons"] = True
        user = fixtures.interaction()
        progress = NS(edit=AsyncMock(), delete=AsyncMock())
        user.edit_original_response.return_value = progress
        user.followup.send.return_value = NS(edit=AsyncMock())
        await bot.compare_command.callback(user, "question", "test/model", "test/vision:vision")
        stages = [call.kwargs.get("content", "") for call in progress.edit.call_args_list]
        self.assertTrue(any("Head to head · 1/2" in stage for stage in stages))
        self.assertTrue(any("Head to head · 2/2" in stage for stage in stages))
        bot.recent_requests[(123, 10)].controls.stop()

    async def test_embed_footer_preserves_answer_and_retries_reset_ui(self):
        request = self.request()
        request.answer_message = NS(edit=AsyncMock())
        request.answer_embed = discord.Embed(description="answer")
        request.answer_embed.set_footer(text="Generation interrupted")
        request.output = "answer"
        await bot.with_response_controls(request, self.config, AsyncMock(), AsyncMock())
        embed = request.answer_message.edit.call_args.kwargs["embed"]
        self.assertEqual(embed.description, "answer\n\n" + bot.RESPONSE_DIVIDER)
        self.assertIn("interrupted", embed.footer.text)
        self.assertIn("test/model", embed.footer.text)
        retry = bot.copy_for_retry(request)
        self.assertIsNone(retry.answer_message)
        self.assertIsNone(retry.answer_embed)
        self.assertIsNone(retry.controls)

    async def test_edit_refreshes_cached_text_and_attachments_for_future_context(self):
        with patch.object(bot, "edited_message_ids", {}), patch.object(bot, "discord_bot", NS(user=NS(id=42))):
            channel = NS(id=10, fetch_message=AsyncMock())
            stale = NS(id=100, channel=channel)
            fresh = NS(id=100, channel=channel, content="corrected", attachments=["new file"])
            channel.fetch_message.return_value = fresh
            bot.msg_nodes[100] = bot.MsgNode(role="user", text="old text", images=[{"old": True}], has_bad_links=True)
            async def populate(message, node, config):
                self.assertIs(message, fresh)
                self.assertEqual(node.images, [])
                self.assertFalse(node.has_bad_links)
                node.text = message.content
            await bot.on_raw_message_edit(NS(message_id=100, data={"content": "corrected", "attachments": []}))
            with patch.object(bot, "populate_msg_node", side_effect=populate), patch.object(bot, "set_parent_msg", AsyncMock()):
                messages, _ = await bot.build_reply_chain_messages(stale, self.config, True)
            self.assertEqual(messages[0]["content"], "corrected")
            channel.fetch_message.assert_awaited_once_with(100)

    async def test_edits_during_refresh_stay_marked_and_bot_edits_are_ignored(self):
        with patch.object(bot, "edited_message_ids", {}), patch.object(bot, "discord_bot", NS(user=NS(id=42))):
            await bot.on_raw_message_edit(NS(message_id=100, data={"content": "footer", "author": {"id": "42"}}))
            await bot.on_raw_message_edit(NS(message_id=100, data={"embeds": []}))
            self.assertEqual(bot.edited_message_ids, {})
            channel = NS(id=10)
            message = NS(id=100, channel=channel)
            channel.fetch_message = AsyncMock(return_value=message)
            bot.msg_nodes[100] = bot.MsgNode(role="user", text="old")
            bot.edited_message_ids[100] = None
            async def populate(message, node, config):
                await bot.on_raw_message_edit(NS(message_id=100, data={"content": "newer"}))
                node.text = "intermediate"
            with patch.object(bot, "populate_msg_node", side_effect=populate), patch.object(bot, "set_parent_msg", AsyncMock()):
                await bot.build_reply_chain_messages(message, self.config, False)
            self.assertIn(100, bot.edited_message_ids)

    async def test_help_is_compact_private_permission_checked_and_registered(self):
        user = fixtures.interaction()
        await bot.help_command.callback(user)
        call = user.response.send_message.call_args
        self.assertLessEqual(len(call.args[0]), 2000)
        self.assertTrue(call.kwargs["ephemeral"])
        for value in ("/ask", "/compare", "private", "Reply", "DMs"):
            self.assertIn(value, call.args[0])
        self.assertEqual(bot.help_command.to_dict(bot.discord_bot.tree)["name"], "help")
        self.config["permissions"]["users"]["blocked_ids"] = [123]
        await bot.help_command.callback(user)
        self.assertIn("permission", user.response.send_message.call_args.args[0])

    async def test_streamed_code_splits_keep_raw_history_and_balanced_displays(self):
        text = "Intro\n```python\n" + "print('hello')\n" * 550 + "```\nEnd"
        for chunk_size in (7, 997, 9000):
            rendered = []
            message = self.streaming_message(rendered)
            request = self.request(kind="message")
            request.start_msg = message
            async def stream():
                for index in range(0, len(text), chunk_size):
                    yield NS(choices=[NS(finish_reason=None, delta=NS(content=text[index:index + chunk_size]))])
                yield NS(choices=[NS(finish_reason="stop", delta=NS(content=None))])
            self.client.chat.completions.create.return_value = stream()
            bot.msg_nodes.clear()
            with patch.object(bot, "build_reply_chain_messages", AsyncMock(return_value=([{"role": "user", "content": "question"}], set()))):
                await bot.send_streaming_reply(message, self.config | {"long_answer_threshold": 0}, request=request)
            self.assertEqual(request.output, text)
            self.assertTrue(all(node.text == text for node in bot.msg_nodes.values()))
            for item in rendered:
                self.assertLessEqual(len(item.data.description), 4096)
                self.assertEqual(len(re.findall(r"(?m)^```(?:python)?\s*$", item.data.description)) % 2, 0)

    async def test_cancelled_edit_fetch_keeps_invalidation(self):
        with patch.object(bot, "edited_message_ids", {100: None}):
            message = NS(id=100, channel=NS(id=10, fetch_message=AsyncMock(side_effect=asyncio.CancelledError())))
            bot.msg_nodes[100] = bot.MsgNode(role="user", text="old")
            with self.assertRaises(asyncio.CancelledError):
                await bot.build_reply_chain_messages(message, self.config, False)
            self.assertIn(100, bot.edited_message_ids)
            self.assertFalse(bot.msg_nodes[100].lock.locked())


if __name__ == "__main__":
    unittest.main()
