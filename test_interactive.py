"""Offline coverage for interactive answers, blind battles, and debate rounds."""
from copy import deepcopy
import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import llmcord as bot
import test_features as fixtures


class InteractiveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FeatureTests.asyncSetUp
    request = fixtures.FeatureTests.request

    async def asyncTearDown(self):
        for request in bot.recent_requests.values():
            if request.controls:
                request.controls.stop()

    def user(self):
        user = fixtures.interaction()
        user.response.edit_message = AsyncMock()
        user.response.send_modal = AsyncMock()
        user.original_response = AsyncMock(return_value=NS(edit=AsyncMock()))
        user.followup.send.return_value = NS(edit=AsyncMock(), delete=AsyncMock())
        return user

    async def test_answer_actions_preserve_private_context_and_original_snapshot(self):
        self.config['response_buttons'] = True
        user = self.user()
        await bot.ask_command.callback(user, 'original question', private=True)
        previous = bot.recent_requests[(123, 10)]
        snapshot = deepcopy(previous.messages)
        actions = {item.label: item for item in previous.controls.children}
        self.assertEqual(set(actions), {'Download', 'Go deeper', 'Shorten', 'Challenge this', 'Change model'})
        for label in ('Go deeper', 'Shorten', 'Challenge this'):
            await actions[label].callback(user)
            current = bot.recent_requests[(123, 10)]
            self.assertTrue(current.private)
            self.assertEqual(current.messages[:-2], snapshot)
            self.assertEqual(current.messages[-2], {'role': 'assistant', 'content': 'answer'})
            self.assertEqual(current.kind, 'ask')
            current.controls.stop()
        self.assertEqual(previous.messages, snapshot)
        self.assertTrue(all(call.kwargs['ephemeral'] for call in user.followup.send.call_args_list))
        previous.controls.stop()

    async def test_actions_respect_busy_state_and_revoked_permissions(self):
        self.config['response_buttons'] = True
        user = self.user()
        await bot.ask_command.callback(user, 'question')
        previous = bot.recent_requests[(123, 10)]
        action = next(item for item in previous.controls.children if item.label == 'Shorten')
        bot.active_requests[123] = (10, NS())
        await action.callback(user)
        self.assertEqual(self.client.chat.completions.create.await_count, 1)
        bot.active_requests.clear()
        self.config['permissions']['users']['blocked_ids'] = [123]
        self.assertFalse(await previous.controls.interaction_check(user))
        await action.callback(user)
        self.assertEqual(self.client.chat.completions.create.await_count, 1)

    async def test_shorten_resolves_thinking_before_waiting_on_provider(self):
        self.config['response_buttons'] = True
        for private in (False, True):
            previous = self.request()
            previous.private = private
            previous.messages = [{'role': 'user', 'content': 'Explain this'}]
            previous.answer_text = 'A long answer to shorten.'
            previous.completed = True
            controls = bot.ResponseControls(previous)
            controls.finish()
            action = next(item for item in controls.children if item.label == 'Shorten')
            user = self.user()
            entered, release = asyncio.Event(), asyncio.Event()
            async def generate(**kwargs):
                user.edit_original_response.assert_awaited_once()
                self.assertEqual(user.edit_original_response.call_args.kwargs['content'], 'Working.')
                self.assertEqual(user.response.defer.call_args.kwargs['ephemeral'], private)
                user.followup.send.assert_not_awaited()
                self.assertEqual(kwargs['messages'][-2]['content'], previous.answer_text)
                self.assertIn('more concisely', kwargs['messages'][-1]['content'])
                entered.set()
                await release.wait()
                return NS(choices=[NS(message=NS(content='Short answer.'))])
            self.client.chat.completions.create.side_effect = generate
            task = asyncio.create_task(action.callback(user))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                release.set()
                await asyncio.wait_for(task, 2)
                self.assertIn('Short answer.', user.followup.send.call_args.args[0])
                self.assertEqual(user.followup.send.call_args.kwargs['ephemeral'], private)
                user.edit_original_response.return_value.delete.assert_awaited_once()
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                controls.stop()
                if (current := bot.recent_requests.get((123, 10))) and current.controls:
                    current.controls.stop()

    async def test_rejected_shorten_resolves_deferred_response(self):
        previous = self.request()
        previous.completed = True
        controls = bot.ResponseControls(previous)
        controls.finish()
        user = self.user()
        bot.active_requests[123] = (10, NS())
        try:
            action = next(item for item in controls.children if item.label == 'Shorten')
            await action.callback(user)
            self.assertIn('already have', user.edit_original_response.call_args.kwargs['content'])
            self.client.chat.completions.create.assert_not_awaited()
            user.followup.send.assert_not_awaited()
        finally:
            bot.active_requests.clear()
            controls.stop()

    async def test_followup_history_is_bounded_without_mutating_source(self):
        previous = self.request()
        previous.messages = [{'role': 'system', 'content': 'rules'}] + [{'role': 'user', 'content': str(i)} for i in range(30)]
        previous.answer_text = 'x' * 200
        snapshot = deepcopy(previous.messages)
        request = bot.followup_request(previous, 'continue', self.config | {'max_messages': 4, 'max_text': 50})
        self.assertEqual(len(request.messages), 5)
        self.assertEqual(request.messages[0]['content'], 'rules')
        self.assertEqual(len(request.messages[-2]['content']), 50)
        self.assertTrue(request.warnings)
        self.assertEqual(previous.messages, snapshot)

    async def test_picker_paginates_and_revalidates_removed_models(self):
        previous = self.request()
        previous.private = True
        previous.messages = [{'role': 'user', 'content': 'question'}]
        models = ['test/' + str(i) for i in range(27)]
        picker = bot.ModelPicker(previous, models)
        user = self.user()
        try:
            self.assertEqual(len(picker.children[0].options), 25)
            await picker.children[2].callback(user)
            self.assertEqual(len(picker.children[0].options), 2)
            self.assertTrue(picker.children[2].disabled)
            picker.children[0]._values = ['25']
            await picker.children[0].callback(user)
            self.client.chat.completions.create.assert_not_awaited()
            self.config['models'][models[25]] = {}
            await picker.children[0].callback(user)
            current = bot.recent_requests[(123, 10)]
            self.assertEqual(current.model, models[25])
            self.assertTrue(current.private)
            self.assertEqual(current.messages, previous.messages)
            self.assertEqual(bot.curr_model, 'test/model')
            self.assertFalse(await picker.interaction_check(fixtures.interaction(user_id=456)))
        finally:
            picker.stop()

    async def test_battle_hides_labels_until_vote_and_cannot_vote_twice(self):
        user = self.user()
        await bot.battle_command.callback(user, 'question', private=True)
        request = bot.recent_requests[(123, 10)]
        # Battle controls stay available even when ordinary answer controls are disabled.
        self.assertIsNotNone(request.controls)
        self.assertEqual(self.client.chat.completions.create.await_count, 2)
        calls = self.client.chat.completions.create.call_args_list
        self.assertEqual(calls[0].kwargs['messages'], calls[1].kwargs['messages'])
        for model in (request.model, request.second_model):
            self.assertNotIn(model, request.output)
            self.assertNotIn(model, bot.response_footer(request))
        self.assertIn('Answer A', request.output)
        vote = next(item for item in request.controls.children if item.label == 'Vote A')
        elapsed = bot.response_footer(request).rsplit(' · ', 1)[-1]
        with patch.object(bot.time, 'monotonic', return_value=request.started_at + 500):
            await vote.callback(user)
            self.assertEqual(bot.response_footer(request).rsplit(' · ', 1)[-1], elapsed)
        self.assertTrue(request.revealed)
        self.assertIn(request.model, user.followup.send.call_args.args[0])
        self.assertIn(request.second_model, user.followup.send.call_args.args[0])
        self.assertTrue(user.followup.send.call_args.kwargs['ephemeral'])
        count = user.followup.send.await_count
        await vote.callback(user)
        self.assertEqual(user.followup.send.await_count, count)
        retry = bot.copy_for_retry(request)
        self.assertFalse(retry.revealed)
        self.assertFalse(retry.completed)
        self.assertIsNone(retry.elapsed_seconds)

    async def test_failed_battle_hides_provider_error_and_disables_voting(self):
        self.client.chat.completions.create.side_effect = [ValueError('secret model name'), NS(choices=[NS(message=NS(content='answer'))])]
        with self.assertLogs(level='ERROR'):
            await bot.battle_command.callback(self.user(), 'question')
        request = bot.recent_requests[(123, 10)]
        self.assertFalse(request.completed)
        self.assertNotIn('secret model', request.output)
        self.assertEqual([item.label for item in request.controls.children], ['Download'])

    async def test_debate_second_model_sees_first_and_modal_continues_private_round(self):
        user = self.user()
        await bot.debate_command.callback(user, 'topic', 'test/model', 'test/vision:vision', private=True)
        previous = bot.recent_requests[(123, 10)]
        calls = self.client.chat.completions.create.call_args_list
        self.assertIn('Debater 1: answer', str(calls[1].kwargs['messages']))
        self.assertNotIn('Debater 1: answer', str(calls[0].kwargs['messages']))
        snapshot = deepcopy(previous.messages)
        steer = next(item for item in previous.controls.children if item.label == 'Steer next round')
        await steer.callback(user)
        modal = user.response.send_modal.call_args.args[0]
        modal.direction._value = 'Focus on costs'
        try:
            await modal.on_submit(user)
            current = bot.recent_requests[(123, 10)]
            self.assertEqual(current.round_number, 2)
            self.assertIn('Round 2', current.output)
            self.assertTrue(current.private)
            self.assertEqual(current.messages[-1]['content'], 'Focus on costs')
            self.assertEqual(current.second_model, previous.second_model)
            self.assertEqual(previous.messages, snapshot)
            self.config['permissions']['users']['blocked_ids'] = [123]
            await modal.on_submit(user)
            self.assertEqual(self.client.chat.completions.create.await_count, 4)
        finally:
            modal.stop()
            previous.controls.stop()

    async def test_failed_debate_keeps_first_answer_without_next_round_button(self):
        self.client.chat.completions.create.side_effect = [NS(choices=[NS(message=NS(content='first argument'))]), TimeoutError()]
        with self.assertLogs(level='ERROR'):
            await bot.debate_command.callback(self.user(), 'topic', 'test/model', 'test/vision:vision')
        request = bot.recent_requests[(123, 10)]
        self.assertIn('first argument', request.output)
        self.assertFalse(request.completed)
        self.assertEqual([item.label for item in request.controls.children], ['Download'])

    async def test_new_command_validation_and_schemas(self):
        user = self.user()
        for command in (bot.battle_command, bot.debate_command):
            schema = command.to_dict(bot.discord_bot.tree)
            self.assertLessEqual(len(schema['description']), 100)
            self.assertIn('private', [option['name'] for option in schema['options']])
        await bot.debate_command.callback(user, 'topic', 'test/model', 'test/model')
        self.config['models'] = {'test/model': {}}
        await bot.battle_command.callback(user, 'question')
        self.client.chat.completions.create.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
