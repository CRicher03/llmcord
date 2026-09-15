"""Regression checks for buttons, and answer files."""
import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import llmcord as bot
import test_features as fixtures


class ResponseToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FeatureTests.asyncSetUp
    request = fixtures.FeatureTests.request
    streaming_message = fixtures.FeatureTests.streaming_message

    async def test_compare_command_is_removed(self):
        self.assertIsNone(bot.discord_bot.tree.get_command("compare"))

    async def test_long_answer_file_contains_exact_unicode_and_code(self):
        user = fixtures.interaction()
        request = self.request()
        request.private = True
        text = "日本語\n```python\nprint('hello')\n```\n" * 1000
        await bot.publish_answer(user, request, text, self.config)
        call = user.followup.send.call_args
        self.assertEqual(call.kwargs["file"].fp.read().decode("utf-8"), text)
        self.assertEqual(call.kwargs["file"].filename, "answer.md")
        self.assertTrue(call.kwargs["ephemeral"])
        self.assertLess(len(call.kwargs.get("content", call.args[0] if call.args else "")), 2000)
        self.assertEqual(request.output, text)

    async def test_file_limit_and_disabled_threshold_fall_back_to_inline(self):
        text = "x" * 7000
        for limit, threshold in [(1, 6000), (1000000, 0)]:
            user = fixtures.interaction()
            user.filesize_limit = limit
            await bot.publish_answer(user, self.request(), text, self.config | {"long_answer_threshold": threshold})
            self.assertEqual("".join(call.args[0] for call in user.followup.send.call_args_list), text)
            self.assertTrue(all("file" not in call.kwargs for call in user.followup.send.call_args_list))

    async def test_controls_authorization(self):
        request = self.request()
        request.private = True
        request.output = "exact answer"
        request.messages = [{"role": "user", "content": "original request"}]
        view = bot.ResponseControls(request)
        try:
            self.assertFalse(await view.interaction_check(fixtures.interaction(user_id=456)))
            self.assertFalse(await view.interaction_check(fixtures.interaction(channel_id=20)))
            owner = fixtures.interaction()
            self.assertTrue(await view.interaction_check(owner))
        finally:
            view.stop()

    async def test_ordinary_response_has_no_buttons(self):
        view = bot.ResponseControls(self.request())
        try:
            self.assertEqual([item.label for item in view.children], [])
            self.assertEqual(view.to_components(), [])
        finally:
            view.stop()

    async def test_controls_lifecycle_and_stop_cancel_active_generation(self):
        self.config["response_buttons"] = True
        ready = asyncio.Event()
        request = self.request()
        control_message = NS(edit=AsyncMock())
        views = []
        async def send(*args, **kwargs):
            views.append(kwargs["view"])
            return control_message
        async def operation():
            ready.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(bot.run_generation(request, self.config, lambda: bot.with_response_controls(request, self.config, operation, send), AsyncMock()))
        await ready.wait()
        view = views[0]
        try:
            self.assertEqual(view.children, [])
            await bot.stop_command.callback(fixtures.interaction())
            self.assertFalse(await task)
            self.assertEqual(view.children, [])
            self.assertEqual(bot.active_requests, {})
            await view.on_timeout()
            self.assertTrue(all(item.disabled for item in view.children))
        finally:
            view.stop()

    async def test_successful_private_ask_has_no_buttons(self):
        self.config["response_buttons"] = True
        user = fixtures.interaction()
        user.followup.send.return_value = NS(edit=AsyncMock())
        await bot.ask_command.callback(user, "question", private=True)
        first = user.edit_original_response.call_args
        view = first.kwargs["view"]
        try:
            self.assertTrue(user.response.defer.call_args.kwargs["ephemeral"])
            self.assertEqual(view.children, [])
            self.assertIn("answer", view.request.output)
            self.assertEqual(view.to_components(), [])
        finally:
            view.stop()

    async def test_new_commands_register_valid_schemas(self):
        for command in (bot.battle_command, bot.debate_command):
            schema = command.to_dict(bot.discord_bot.tree)
            self.assertLessEqual(len(schema["description"]), 100)
            self.assertTrue(any(option["name"] == "private" for option in schema["options"]))

    async def test_long_mention_replies_attach_files_and_preserve_history_in_both_modes(self):
        for plain in (True, False):
            with self.subTest(plain=plain):
                bot.msg_nodes.clear()
                message = self.streaming_message([])
                sent = []
                async def reply(*args, **kwargs):
                    sent.append(kwargs)
                    return NS(id=2000 + len(sent), reply=reply, edit=AsyncMock())
                message.reply = reply
                async def stream():
                    yield NS(choices=[NS(finish_reason="stop", delta=NS(content="x" * 9000))])
                self.client.chat.completions.create.return_value = stream()
                request = self.request(kind="message")
                request.start_msg = message
                with patch.object(bot, "build_reply_chain_messages", AsyncMock(return_value=([{"role": "user", "content": "question"}], set()))):
                    await bot.send_streaming_reply(message, self.config | {"use_plain_responses": plain}, request=request)
                files = [call["file"] for call in sent if "file" in call]
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].fp.read(), b"x" * 9000)
                self.assertEqual(request.output, "x" * 9000)
                self.assertTrue(all(node.text == request.output for node in bot.msg_nodes.values()))
                self.assertTrue(all(not node.lock.locked() for node in bot.msg_nodes.values()))

    async def test_long_mention_replies_attach_files_and_preserve_history_in_both_modes(self):
        for plain in (True, False):
            with self.subTest(plain=plain):
                bot.msg_nodes.clear()
                message = self.streaming_message([])
                sent = []
                async def reply(*args, **kwargs):
                    sent.append(kwargs)
                    return NS(id=2000 + len(sent), reply=reply, edit=AsyncMock())
                message.reply = reply
                async def stream():
                    yield NS(choices=[NS(finish_reason="stop", delta=NS(content="x" * 9000))])
                self.client.chat.completions.create.return_value = stream()
                request = self.request(kind="message")
                request.start_msg = message
                with patch.object(bot, "build_reply_chain_messages", AsyncMock(return_value=([{"role": "user", "content": "question"}], set()))):
                    await bot.send_streaming_reply(message, self.config | {"use_plain_responses": plain}, request=request)
                files = [call["file"] for call in sent if "file" in call]
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].fp.read(), b"x" * 9000)
                self.assertEqual(request.output, "x" * 9000)
                self.assertTrue(all(node.text == request.output for node in bot.msg_nodes.values()))
                self.assertTrue(all(not node.lock.locked() for node in bot.msg_nodes.values()))


if __name__ == "__main__":
    unittest.main()
