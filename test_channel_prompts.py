"""Offline coverage for channel prompt routing and saved overrides."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import llmcord as bot
import test_features as fixtures


class ChannelPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.FeatureTests.asyncSetUp(self)
        patcher = patch.object(bot, "channel_prompts", {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config["persist_channel_prompts"] = False

    def test_precedence_empty_prompt_and_placeholders(self):
        self.config["channel_prompts"] = {10: "Tutor {date}", "11": "Thread tutor"}
        self.assertEqual(bot.get_system_prompt(self.config, 99), "Original system prompt")
        self.assertNotIn("{date}", bot.get_system_prompt(self.config, 12, 10))
        bot.channel_prompts[10] = "Parent override"
        self.assertEqual(bot.get_system_prompt(self.config, 12, 10), "Parent override")
        self.assertEqual(bot.get_system_prompt(self.config, 11, 10), "Thread tutor")
        bot.channel_prompts[11] = ""
        self.assertEqual(bot.get_system_prompt(self.config, 11, 10), "")

    async def test_admin_set_status_reset_and_validation(self):
        await bot.channel_prompt_command.callback(fixtures.interaction(), "Tutor")
        self.assertEqual(bot.channel_prompts, {})
        for prompt, reset, guild in [("", False, True), (" " , False, True), ("x" * 4001, False, True), ("Tutor", True, True), ("Tutor", False, False)]:
            await bot.channel_prompt_command.callback(fixtures.interaction(999, guild=guild), prompt, reset)
            self.assertEqual(bot.channel_prompts, {})
        admin = fixtures.interaction(999)
        await bot.channel_prompt_command.callback(admin, "Tutor")
        self.assertEqual(bot.channel_prompts, {10: "Tutor"})
        await bot.channel_prompt_command.callback(admin)
        self.assertIn("command override", admin.response.send_message.call_args.args[0])
        self.assertTrue(admin.response.send_message.call_args.kwargs["ephemeral"])
        await bot.channel_prompt_command.callback(admin, reset=True)
        self.assertEqual(bot.channel_prompts, {})

    async def test_slash_thread_routing_and_retry_snapshot(self):
        bot.channel_prompts[10] = "Educational tutor"
        user = fixtures.interaction(channel_id=11)
        user.channel.parent_id = 10
        await bot.ask_command.callback(user, "Explain fractions")
        messages = self.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "Educational tutor"})
        previous = bot.recent_requests[(123, 11)]
        bot.channel_prompts[10] = "Changed tutor"
        retry = bot.copy_for_retry(previous)
        await bot.prepare_ask_request(retry, self.config)
        self.assertEqual(retry.messages[0]["content"], "Educational tutor")

    async def test_streaming_chat_routing(self):
        message = fixtures.FeatureTests.streaming_message(self, [])
        async def stream():
            if False:
                yield
        self.client.chat.completions.create.return_value = stream()
        bot.channel_prompts[message.channel.id] = "Educational tutor"
        with patch.object(bot, "build_reply_chain_messages", AsyncMock(return_value=([{"role": "user", "content": "question"}], set()))):
            await bot.send_streaming_reply(message, self.config)
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["messages"][0]["content"], "Educational tutor")

    async def test_persistence_reset_corruption_and_save_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.json"
            self.config.update(persist_channel_prompts=True, prompt_state_file=str(path))
            await bot.channel_prompt_command.callback(fixtures.interaction(999), "Tutor")
            bot.channel_prompts.clear()
            bot.restore_channel_prompts(self.config)
            self.assertEqual(bot.channel_prompts, {10: "Tutor"})
            await bot.channel_prompt_command.callback(fixtures.interaction(999), reset=True)
            self.assertEqual(json.loads(path.read_text()), {})
            path.write_text("[]")
            with self.assertLogs(level="ERROR"):
                bot.restore_channel_prompts(self.config)
            with patch.object(bot.os, "replace", side_effect=PermissionError()), self.assertLogs(level="ERROR"):
                self.assertIn("saving failed", bot.save_channel_prompts(self.config))
            self.assertEqual(path.read_text(), "[]")
