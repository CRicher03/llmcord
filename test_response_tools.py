"""Regression checks for comparisons, summaries, buttons, and answer files."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import llmcord as bot
import test_features as fixtures


class ResponseToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FeatureTests.asyncSetUp
    request = fixtures.FeatureTests.request
    streaming_message = fixtures.FeatureTests.streaming_message

    async def test_comparison_has_identical_inputs_labeled_outputs_and_private_delivery(self):
        self.client.chat.completions.create.side_effect = [NS(choices=[NS(message=NS(content="first answer"))]), NS(choices=[NS(message=NS(content="second answer"))])]
        user = fixtures.interaction()
        await bot.compare_command.callback(user, "question", "test/model", "test/vision:vision", private=True)
        calls = self.client.chat.completions.create.await_args_list
        self.assertEqual(calls[0].kwargs["messages"], calls[1].kwargs["messages"])
        self.assertEqual([call.kwargs["model"] for call in calls], ["model", "vision"])
        output = user.followup.send.call_args.args[0]
        self.assertIn("## test/model", output)
        self.assertIn("## test/vision:vision", output)
        self.assertIn("second answer", output)
        self.assertTrue(user.followup.send.call_args.kwargs["ephemeral"])
        self.assertEqual(bot.active_requests, {})

    async def test_comparison_retains_success_when_other_model_fails(self):
        self.client.chat.completions.create.side_effect = [TimeoutError(), NS(choices=[NS(message=NS(content="working answer"))])]
        user = fixtures.interaction()
        with self.assertLogs(level="ERROR"):
            await bot.compare_command.callback(user, "question", "test/model", "test/vision:vision")
        output = user.followup.send.call_args.args[0]
        self.assertIn("timed out", output)
        self.assertIn("working answer", output)
        self.assertEqual(self.client.close.await_count, 2)

    async def test_compare_validation_and_permission_checks(self):
        for models in [("test/model", "test/model"), ("test/model", "missing")]:
            await bot.compare_command.callback(fixtures.interaction(), "question", *models)
        self.config["permissions"]["users"]["blocked_ids"] = [123]
        await bot.compare_command.callback(fixtures.interaction(), "question", "test/model", "test/vision:vision")
        self.client.chat.completions.create.assert_not_awaited()

    async def test_compare_retry_keeps_both_models_and_clears_old_output(self):
        await bot.compare_command.callback(fixtures.interaction(), "question", "test/model", "test/vision:vision")
        original = bot.recent_requests[(123, 10)]
        await bot.retry_command.callback(fixtures.interaction())
        retry = bot.recent_requests[(123, 10)]
        self.assertEqual(retry.second_model, "test/vision:vision")
        self.assertEqual(retry.messages, original.messages)
        self.assertEqual(self.client.chat.completions.create.await_count, 4)
        self.assertEqual(bot.copy_for_retry(retry).output, "")

    async def test_summary_reads_selected_chain_and_retains_snapshot_for_retry(self):
        user = fixtures.interaction()
        user.permissions = NS(view_channel=True, read_message_history=True)
        user.channel.fetch_message = AsyncMock(return_value=NS(id=100))
        chain = [{"role": "user", "content": "What is next?"}, {"role": "assistant", "content": "We agreed to ship Friday."}]
        builder = AsyncMock(return_value=(chain, {"Warning: truncated history"}))
        with patch.object(bot, "build_reply_chain_messages", builder):
            await bot.summarize_command.callback(user, "https://discord.com/channels/1/10/100", private=True)
            first = deepcopy(self.client.chat.completions.create.call_args.kwargs["messages"])
            await bot.retry_command.callback(user)
        user.channel.fetch_message.assert_awaited_once_with(100)
        builder.assert_awaited_once()
        self.assertTrue(builder.call_args.kwargs["same_channel_only"])
        self.assertIn("decisions", first[0]["content"])
        self.assertIn("ship Friday", first[1]["content"])
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["messages"], first)
        self.assertIn("truncated history", user.followup.send.call_args.args[0])
        self.assertTrue(user.followup.send.call_args.kwargs["ephemeral"])

    async def test_summary_rejects_cross_channel_links_and_missing_history_access(self):
        user = fixtures.interaction()
        user.channel.fetch_message = AsyncMock()
        await bot.summarize_command.callback(user, "https://discord.com/channels/1/20/100")
        user.channel.fetch_message.assert_not_awaited()
        user.permissions = NS(view_channel=True, read_message_history=False)
        with self.assertLogs(level="ERROR"):
            await bot.summarize_command.callback(user, "100")
        user.channel.fetch_message.assert_not_awaited()
        self.client.chat.completions.create.assert_not_awaited()

    async def test_summary_chain_stops_before_cross_channel_parent(self):
        parent = NS(id=2, channel=NS(id=20))
        source = NS(id=1, channel=NS(id=10))
        bot.msg_nodes[1] = bot.MsgNode(text="current channel", role="user", parent_msg=parent)
        messages, warnings = await bot.build_reply_chain_messages(source, self.config, False, same_channel_only=True)
        self.assertEqual(len(messages), 1)
        self.assertNotIn(2, bot.msg_nodes)
        self.assertTrue(any("outside" in text for text in warnings))

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

    async def test_controls_authorization_download_and_old_response_retry(self):
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
            await view.download_response.callback(owner)
            call = owner.response.send_message.call_args
            self.assertEqual(call.kwargs["file"].fp.read(), b"exact answer")
            self.assertTrue(call.kwargs["ephemeral"])
            bot.recent_requests[(123, 10)] = self.request()
            bot.recent_requests[(123, 10)].prompt = "new unrelated question"
            with patch.object(bot, "run_interaction_request", AsyncMock()) as run:
                await view.retry_response.callback(owner)
            retried = run.call_args.args[1]
            self.assertEqual(retried.messages, request.messages)
            self.assertTrue(retried.private)
            self.assertEqual(retried.output, "")
        finally:
            view.stop()

    async def test_old_stop_button_cannot_cancel_new_task(self):
        view = bot.ResponseControls(self.request())
        newer = asyncio.create_task(asyncio.sleep(60))
        bot.active_requests[123] = (10, newer)
        try:
            await view.stop_generation.callback(fixtures.interaction())
            self.assertFalse(newer.cancelling())
        finally:
            view.stop()
            newer.cancel()
            try:
                await newer
            except asyncio.CancelledError:
                pass

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
            self.assertFalse(view.stop_generation.disabled)
            self.assertTrue(view.retry_response.disabled)
            await view.stop_generation.callback(fixtures.interaction())
            self.assertFalse(await task)
            self.assertTrue(view.stop_generation.disabled)
            self.assertFalse(view.retry_response.disabled)
            self.assertEqual(bot.active_requests, {})
            await view.on_timeout()
            self.assertTrue(all(item.disabled for item in view.children))
        finally:
            view.stop()

    async def test_successful_private_ask_controls_enable_download(self):
        self.config["response_buttons"] = True
        user = fixtures.interaction()
        user.followup.send.return_value = NS(edit=AsyncMock())
        await bot.ask_command.callback(user, "question", private=True)
        first = user.followup.send.call_args_list[0]
        view = first.kwargs["view"]
        try:
            self.assertTrue(first.kwargs["ephemeral"])
            self.assertFalse(view.download_response.disabled)
            self.assertTrue(view.stop_generation.disabled)
            self.assertIn("answer", view.request.output)
            self.assertEqual(len(view.to_components()[0]["components"]), 3)
        finally:
            view.stop()

    async def test_new_commands_register_valid_schemas(self):
        for command in (bot.compare_command, bot.summarize_command):
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
