"""Offline regression tests: python -m unittest -v test_llmcord."""

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
import yaml

import llmcord as bot


class PermissionTests(unittest.TestCase):
    def test_supported_id_formats(self):
        for value, expected in [(None, []), ("", []), ("  ", []), ([], []), (123, [123]),
                                ("123", [123]), ("123,456", [123, 456]),
                                ('[123,"456"]', [123, 456]), ([123, "456"], [123, 456])]:
            with self.subTest(value=value):
                self.assertEqual(bot.normalize_permission_ids(value), expected)

    def test_invalid_ids_fail_instead_of_granting_access(self):
        for value in [True, 0, -1, 1.5, "abc", [123, "bad"], {}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bot.normalize_permission_ids(value)

    def test_config_normalizes_env_and_preserves_permission_behavior(self):
        config = {"allow_dms": True, "permissions": {"users": {"allowed_ids_env": "TEST_ALLOWED_IDS", "blocked_ids": None}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            with patch.dict("os.environ", {"TEST_ALLOWED_IDS": "123"}):
                config = bot.get_config(str(path))
        channel = NS(type=discord.ChannelType.private, id=99)
        for user_id, allowed in [(123, True), (456, False)]:
            user = NS(id=user_id)
            self.assertEqual(bot.user_has_permission_for_message(NS(channel=channel, author=user), config), allowed)
            self.assertEqual(bot.user_has_permission_for_interaction(NS(channel=channel, user=user, guild=None), config), allowed)
        config["permissions"]["users"]["blocked_ids"] = [123]
        self.assertFalse(bot.user_has_permission_for_message(NS(channel=channel, author=NS(id=123)), config))
        config["permissions"]["users"]["admin_ids"] = [123]
        self.assertTrue(bot.user_has_permission_for_message(NS(channel=channel, author=NS(id=123)), config))


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.client_options = []
        self.responses = []
        self.real_client = httpx.AsyncClient
        self.client_patch = patch.object(bot.httpx, "AsyncClient", side_effect=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.loop = asyncio.get_running_loop()
        self.dns = AsyncMock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
        self.dns_patch = patch.object(self.loop, "getaddrinfo", self.dns)
        self.dns_patch.start()
        self.addCleanup(self.dns_patch.stop)
        self.slots_patch = patch.object(bot, "download_slots", asyncio.Semaphore(4))
        self.slots_patch.start()
        self.addCleanup(self.slots_patch.stop)

    def client(self, **kwargs):
        self.client_options.append(kwargs)
        return self.real_client(transport=httpx.MockTransport(self.handle), **kwargs)

    def handle(self, request):
        self.requests.append(request)
        return self.responses.pop(0)

    def response(self, chunks=(b"hello",), **kwargs):
        stream = Chunks(chunks)
        self.responses.append(httpx.Response(kwargs.pop("status", 200), stream=stream, **kwargs))
        return stream

    async def test_public_https_preserves_host_tls_and_original_url(self):
        stream = self.response(headers={"content-type": "text/plain"})
        response = await bot.download_context("https://public.example:8443/file.txt?q=yes", {})
        self.assertEqual(response.text, "hello")
        self.assertEqual(str(response.url), "https://public.example:8443/file.txt?q=yes")
        request = self.requests[0]
        self.assertEqual(request.url.host, "93.184.216.34")
        self.assertEqual(request.headers["host"], "public.example:8443")
        self.assertEqual(request.extensions["sni_hostname"], "public.example")
        self.assertFalse(self.client_options[0]["trust_env"])
        self.assertTrue(stream.closed)

    async def test_private_literals_and_invalid_urls_make_no_request(self):
        for url in ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/", "http://10.0.0.1/", "http://localhost./", "file:///etc/passwd", "https://user:pass@public.example/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                await bot.download_context(url, {})
        self.assertEqual(self.requests, [])

    async def test_private_and_mixed_dns_answers_are_rejected(self):
        for addresses in [["127.0.0.1"], ["10.0.0.1"], ["93.184.216.34", "10.0.0.1"]]:
            self.dns.return_value = [(2, 1, 6, "", (address, 443)) for address in addresses]
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                await bot.download_context("https://public.example/", {})
        self.assertEqual(self.requests, [])

    async def test_redirect_to_private_address_is_rejected_before_connecting(self):
        self.response(status=302, headers={"location": "http://127.0.0.1/private"})
        with self.assertRaises(ValueError):
            await bot.download_context("https://public.example/", {})
        self.assertEqual(len(self.requests), 1)

    async def test_redirect_hostname_is_resolved_again(self):
        self.response(status=302, headers={"location": "https://internal.example/private"})
        self.dns.side_effect = [[(2, 1, 6, "", ("93.184.216.34", 443))], [(2, 1, 6, "", ("10.0.0.1", 443))]]
        with self.assertRaises(ValueError):
            await bot.download_context("https://public.example/", {})
        self.assertEqual(len(self.requests), 1)

    async def test_relative_redirect_and_redirect_limit(self):
        self.response(status=302, headers={"location": "/article"})
        self.response()
        result = await bot.download_context("https://public.example/start", {})
        self.assertEqual(str(result.url), "https://public.example/article")
        for _ in range(6):
            self.response(status=302, headers={"location": "/loop"})
        with self.assertRaises(ValueError):
            await bot.download_context("https://public.example/loop", {})

    async def test_byte_limit_with_and_without_content_length(self):
        declared = self.response(headers={"content-length": "100"})
        with self.assertRaises(ValueError):
            await bot.download_context("https://public.example/", {"max_download_bytes": 10})
        self.assertEqual(declared.read_count, 0)
        streamed = self.response(chunks=[b"x" * 65536, b"y" * 65536, b"unread"])
        with self.assertRaises(ValueError):
            await bot.download_context("https://public.example/", {"max_download_bytes": 65536})
        self.assertEqual(streamed.read_count, 2)
        self.assertTrue(streamed.closed)

    async def test_compressed_and_error_responses_are_not_ingested(self):
        for kwargs, error in [({"headers": {"content-encoding": "gzip"}}, ValueError), ({"status": 404}, httpx.HTTPStatusError)]:
            stream = self.response(**kwargs)
            with self.assertRaises(error):
                await bot.download_context("https://public.example/", {})
            self.assertEqual(stream.read_count, 0)

    async def test_zero_url_limit_disables_fetching(self):
        self.assertEqual(await bot.extract_url_texts("https://public.example/", {"max_urls": 0}), ([], False))
        self.assertEqual(self.requests, [])

    async def test_total_timeout_includes_dns(self):
        async def slow_dns(*args, **kwargs):
            await asyncio.sleep(1)
        self.dns.side_effect = slow_dns
        with self.assertRaises(TimeoutError):
            await bot.download_context("https://public.example/", {"url_fetch_timeout": 0.01})


class DocumentTests(unittest.TestCase):
    def test_normal_docx_text_and_limit(self):
        document = Document()
        document.add_paragraph("Hello from a document")
        buffer = io.BytesIO()
        document.save(buffer)
        self.assertEqual(bot.extract_docx_text(buffer.getvalue()), "Hello from a document")
        self.assertEqual(bot.extract_docx_text(buffer.getvalue(), 5), "Hello")
        with self.assertRaises(ValueError):
            bot.extract_docx_text(buffer.getvalue(), max_expanded_bytes=10)

    def test_pdf_page_limit(self):
        for count in [1, 101]:
            writer = PdfWriter()
            for _ in range(count):
                writer.add_blank_page(width=72, height=72)
            buffer = io.BytesIO()
            writer.write(buffer)
            if count == 1:
                self.assertEqual(bot.extract_pdf_text(buffer.getvalue()), "")
            else:
                with self.assertRaises(ValueError):
                    bot.extract_pdf_text(buffer.getvalue())
                self.assertEqual(bot.extract_pdf_text(buffer.getvalue(), max_pages=101), "")


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def run_reply(self, plain, pieces, finish="stop", tail="tail"):
        sent = []

        @asynccontextmanager
        async def typing():
            yield

        class Message:
            channel = NS(typing=typing)
            author = NS(id=123)
            content = "test"
            attachments = []

            async def reply(self, **kwargs):
                item = Message()
                item.id = len(sent) + 1
                item.data = kwargs["view"].children[0].content if plain else deepcopy(kwargs["embed"])
                sent.append(item)
                return item

            async def edit(self, **kwargs):
                self.data = deepcopy(kwargs["embed"])

        async def chunks():
            yield NS(choices=[])
            yield NS(choices=[NS(finish_reason=None, delta=NS(content=None))])
            for piece in pieces:
                yield NS(choices=[NS(finish_reason=None, delta=NS(content=piece))])
            yield NS(choices=[NS(finish_reason=finish, delta=NS(content=tail))])

        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=chunks()))), close=AsyncMock())
        config = {"providers": {"test": {"base_url": "https://provider.example"}}, "models": {}, "use_plain_responses": plain}
        with patch.object(bot, "AsyncOpenAI", return_value=client), patch.object(bot, "get_effective_model", return_value="test/model"), patch.object(bot, "build_reply_chain_messages", AsyncMock(return_value=([], set()))), patch.object(bot, "EDIT_DELAY_SECONDS", 0), patch.object(bot, "msg_nodes", {}):
            await bot.send_streaming_reply(Message(), config)
            texts = [item.data if plain else item.data.description for item in sent]
            self.assertEqual("".join(texts), "".join(pieces) + (tail or ""))
            self.assertTrue(all(0 < len(text) <= (4000 if plain else 4096 - len(bot.STREAMING_INDICATOR)) for text in texts))
            self.assertTrue(all(not node.lock.locked() for node in bot.msg_nodes.values()))
            self.assertTrue(all(node.text == "".join(texts) for node in bot.msg_nodes.values()))
            if not plain and sent:
                self.assertEqual(sent[-1].data.color, bot.EMBED_COLOR_COMPLETE if finish == "stop" else bot.EMBED_COLOR_INCOMPLETE)
        client.close.assert_awaited_once()

    async def test_plain_and_embed_splitting_preserve_all_content(self):
        for plain in [True, False]:
            for pieces in [["hello", " world"], ["x" * 9000], ["a" * 3999, "b" * 9000], ["a" * 4000], []]:
                with self.subTest(plain=plain, lengths=[len(piece) for piece in pieces]):
                    await self.run_reply(plain, pieces)

    async def test_non_success_finish_stays_incomplete(self):
        await self.run_reply(False, ["partial response"], finish="length")

    async def test_empty_finish_chunks_and_empty_responses(self):
        for plain in [True, False]:
            for tail in [None, ""]:
                await self.run_reply(plain, ["hello"], tail=tail)
                await self.run_reply(plain, [], tail=tail)

    async def test_attachment_limits_skip_downloads_and_keep_text(self):
        author = NS(id=123)
        attachments = [NS(filename="a.png", content_type="image/png", size=3, url="https://cdn.example/a"),
                       NS(filename="b.png", content_type="image/png", size=3, url="https://cdn.example/b"),
                       NS(filename="big.txt", content_type="text/plain", size=100, url="https://cdn.example/big"),
                       NS(filename="ok.txt", content_type="text/plain", size=5, url="https://cdn.example/ok")]
        message = NS(author=author, content="", attachments=attachments, embeds=[], components=[])
        node = bot.MsgNode()
        download = AsyncMock(side_effect=[httpx.Response(200, content=b"img"), httpx.Response(200, text="hello")])
        with patch.object(bot, "discord_bot", NS(user=NS(mention="<@bot>"))), patch.object(bot, "download_context", download):
            await bot.populate_msg_node(message, node, {"max_images": 1, "max_download_bytes": 10})
        self.assertEqual([call.args[0] for call in download.await_args_list], [attachments[0].url, attachments[3].url])
        self.assertEqual(len(node.images), 1)
        self.assertIn("hello", node.text)
        self.assertTrue(node.has_bad_attachments)


if __name__ == "__main__":
    unittest.main()
