"""Tests for feishu/content.py and the network-free parts of feishu/client.py."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lark_oapi.api.im.v1 import (  # noqa: E402
    CreateFileResponse,
    CreateMessageReactionResponse,
    CreateMessageResponse,
    ReplyMessageResponse,
)

from wiseglow_desk.feishu import client as client_mod  # noqa: E402
from wiseglow_desk.feishu.client import (  # noqa: E402
    FeishuClient,
    FeishuError,
    file_type_for,
    resolve_domain,
    safe_filename,
    timestamped_name,
    unique_path,
)
from wiseglow_desk.feishu.content import (  # noqa: E402
    decode_content,
    looks_like_markdown,
    markdown_card,
    split_text,
    strip_mentions,
    text_content,
)

# ---------------------------------------------------------------------------
# strip_mentions
# ---------------------------------------------------------------------------


def test_strip_mentions_removes_placeholders_and_trims() -> None:
    assert strip_mentions("@_user_1 ciao, mi prepari una NBO?") == "ciao, mi prepari una NBO?"
    assert strip_mentions("ciao @_user_2 e @_user_13 come va") == "ciao e come va"
    assert strip_mentions("@_all attenzione") == "attenzione"
    assert strip_mentions("  solo testo  ") == "solo testo"
    assert strip_mentions("") == ""


def test_strip_mentions_keeps_real_emails_and_lines() -> None:
    assert strip_mentions("scrivi a mario@_user.it\n@_user_1 grazie") == "scrivi a mario@_user.it\ngrazie"


# ---------------------------------------------------------------------------
# split_text
# ---------------------------------------------------------------------------


def test_split_text_short_and_blank() -> None:
    assert split_text("ciao") == ["ciao"]
    assert split_text("   ") == []
    assert split_text("") == []


def test_split_text_prefers_paragraph_boundaries() -> None:
    paragraphs = [f"Paragrafo {i} " + "parola " * 20 for i in range(10)]
    text = "\n\n".join(p.strip() for p in paragraphs)
    chunks = split_text(text, limit=400)
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks)
    # Every chunk starts at a paragraph start and the content is preserved.
    assert all(c.startswith("Paragrafo") for c in chunks)
    assert "\n\n".join(chunks) == text


def test_split_text_never_splits_inside_a_word() -> None:
    words = [f"parola{i}" for i in range(300)]
    text = " ".join(words)
    chunks = split_text(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert " ".join(chunks).split(" ") == words


def test_split_text_falls_back_to_lines_then_words() -> None:
    long_line = "x " * 300  # one paragraph, one line, many words
    text = "intro\n" + long_line.strip() + "\nfine"
    chunks = split_text(text, limit=120)
    assert all(len(c) <= 120 for c in chunks)
    assert chunks[0].startswith("intro")
    assert chunks[-1].endswith("fine")


def test_split_text_hard_cuts_when_no_separator_exists() -> None:
    text = "中" * 250
    chunks = split_text(text, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert "".join(chunks) == text


# ---------------------------------------------------------------------------
# looks_like_markdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "# Titolo\ntesto",
        "## Sezione",
        "Ecco:\n- uno\n- due",
        "1. primo\n2. secondo",
        "Il prezzo è **confermato**",
        "| Voce | Valore |\n|---|---|\n| a | b |",
        "```\ncodice\n```",
        "vedi [qui](https://example.com/x)",
    ],
)
def test_looks_like_markdown_positive(text: str) -> None:
    assert looks_like_markdown(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Ciao Marco, la NBO è pronta - te la mando tra poco.",
        "Prezzo: 100 €/MWh * 2 anni",
        "email: info@wiseglow.it #ufficio",
        "Va bene, procedo con la versione 2.",
    ],
)
def test_looks_like_markdown_negative(text: str) -> None:
    assert not looks_like_markdown(text)


# ---------------------------------------------------------------------------
# markdown_card / text_content
# ---------------------------------------------------------------------------


def test_markdown_card_schema_without_title() -> None:
    card = markdown_card("ciao **mondo**")
    assert card["schema"] == "2.0"
    assert card["config"] == {"wide_screen_mode": True}
    assert card["body"]["elements"] == [{"tag": "markdown", "content": "ciao **mondo**"}]
    assert "header" not in card


def test_markdown_card_with_title_and_heading_conversion() -> None:
    text = "# Riepilogo NBO\n\n## Prezzo\n- 120 €/MWh\n### Note **importanti**\n| a | b |\n|---|---|\n| 1 | 2 |"
    card = markdown_card(text, title="NBO Lecce")
    assert card["header"] == {"title": {"tag": "plain_text", "content": "NBO Lecce"}, "template": "blue"}
    content = card["body"]["elements"][0]["content"]
    assert "**Riepilogo NBO**" in content
    assert "**Prezzo**" in content
    assert "**Note importanti**" in content
    assert "#" not in content
    assert "- 120 €/MWh" in content  # lists kept
    assert "| a | b |" in content and "|---|---|" in content  # tables kept


def test_markdown_card_strips_code_fences_but_keeps_code() -> None:
    card = markdown_card("prima\n```python\nprint(1)\n```\ndopo")
    content = card["body"]["elements"][0]["content"]
    assert "```" not in content
    assert content == "prima\nprint(1)\ndopo"
    assert json.loads(json.dumps(card))  # serialisable


def test_text_content_is_json_with_text_key() -> None:
    payload = text_content("ciao à 中文")
    assert json.loads(payload) == {"text": "ciao à 中文"}


# ---------------------------------------------------------------------------
# decode_content
# ---------------------------------------------------------------------------


def test_decode_text_strips_mentions() -> None:
    decoded = decode_content("text", json.dumps({"text": "@_user_1 fammi una LOI"}))
    assert decoded.text == "fammi una LOI"
    assert decoded.images == [] and decoded.files == []


def test_decode_post_flattens_lines_and_collects_images() -> None:
    post = {
        "title": "Terreno Lecce",
        "content": [
            [{"tag": "text", "text": "Ciao "}, {"tag": "at", "user_id": "@_user_1", "user_name": "Bot"}, {"tag": "text", "text": " guarda"}],
            [{"tag": "a", "text": "planimetria", "href": "https://x.it/p.pdf"}],
            [{"tag": "img", "image_key": "img_abc"}],
        ],
    }
    decoded = decode_content("post", json.dumps(post))
    assert decoded.text == "Terreno Lecce\nCiao guarda\nplanimetria (https://x.it/p.pdf)"
    assert decoded.images == ["img_abc"]


def test_decode_post_locale_wrapped() -> None:
    post = {"zh_cn": {"title": "", "content": [[{"tag": "text", "text": "你好"}]]}}
    assert decode_content("post", json.dumps(post)).text == "你好"


def test_decode_image_and_file() -> None:
    img = decode_content("image", json.dumps({"image_key": "img_1"}))
    assert img.images == ["img_1"] and img.text == ""
    f = decode_content("file", json.dumps({"file_key": "file_1", "file_name": "visura.pdf"}))
    assert f.files == [("file_1", "visura.pdf")]
    f2 = decode_content("file", json.dumps({"file_key": "file_2"}))
    assert f2.files == [("file_2", "file_2.bin")]


def test_decode_unsupported_types_give_italian_text() -> None:
    assert decode_content("audio", json.dumps({"file_key": "a", "duration": 2000})).text == "[messaggio audio non supportato]"
    assert decode_content("sticker", json.dumps({"file_key": "s"})).text == "[sticker non supportato]"
    assert "non supportat" in decode_content("share_calendar_event", "{}").text
    assert "boh" in decode_content("boh", "{}").text


def test_decode_invalid_json_never_raises() -> None:
    assert decode_content("text", "not json @_user_1").text == "not json"
    assert decode_content("text", "").text == ""


# ---------------------------------------------------------------------------
# client pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", ("pdf", None)),
        ("REPORT.PDF", ("pdf", None)),
        ("old.doc", ("doc", None)),
        ("tab.xls", ("xls", None)),
        ("deck.ppt", ("ppt", None)),
        ("clip.mp4", ("mp4", None)),
        ("voice.opus", ("opus", None)),
        ("nbo.docx", ("doc", "stream")),
        ("dati.xlsx", ("xls", "stream")),
        ("slides.pptx", ("ppt", "stream")),
        ("archivio.zip", ("stream", None)),
        ("noext", ("stream", None)),
    ],
)
def test_file_type_for(name: str, expected: tuple[str, str | None]) -> None:
    assert file_type_for(Path(name)) == expected


def test_safe_filename_keeps_allowed_chars_and_cjk() -> None:
    assert safe_filename("Visura catastale (2026).pdf") == "Visura_catastale_2026.pdf"
    assert safe_filename("土地 意向书.docx") == "土地_意向书.docx"
    assert safe_filename("a/b\\c:d*e?.txt") == "c_d_e.txt"  # base name only, unsafe chars -> "_"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("...") == "file"
    assert safe_filename("") == "file"
    assert safe_filename("ok-name_1.PDF") == "ok-name_1.PDF"


def test_safe_filename_caps_length_and_keeps_extension() -> None:
    name = "a" * 200 + ".pdf"
    out = safe_filename(name)
    assert len(out) == 80
    assert out.endswith(".pdf")
    assert safe_filename("b" * 100) == "b" * 80


def test_timestamped_name_and_unique_path(tmp_path: Path) -> None:
    now = datetime(2026, 9, 29, 14, 5, 9)
    assert timestamped_name("Visura LE.pdf", now) == "20260929-140509_Visura_LE.pdf"
    (tmp_path / "x.pdf").write_bytes(b"1")
    assert unique_path(tmp_path, "x.pdf") == tmp_path / "x-1.pdf"
    assert unique_path(tmp_path, "y.pdf") == tmp_path / "y.pdf"


def test_resolve_domain() -> None:
    assert resolve_domain("feishu") == "https://open.feishu.cn"
    assert resolve_domain("Lark") == "https://open.larksuite.com"
    assert resolve_domain(None) == "https://open.feishu.cn"
    assert resolve_domain("https://open.larksuite.com/") == "https://open.larksuite.com"
    with pytest.raises(ValueError):
        resolve_domain("wechat")


# ---------------------------------------------------------------------------
# FeishuClient without network (lark resource methods monkeypatched)
# ---------------------------------------------------------------------------


class _FakeMessages:
    """Stand-in for ``client.im.v1.message`` recording every request."""

    def __init__(self, fail: bool = False, fail_reply: bool = False) -> None:
        self.created: list = []
        self.replied: list = []
        self.fail = fail
        self.fail_reply = fail_reply
        self._n = 0

    def _resp(self, cls, ok: bool):
        self._n += 1
        if ok:
            return cls({"code": 0, "msg": "ok", "data": {"message_id": f"om_{self._n}"}})
        return cls({"code": 230001, "msg": "no permission"})

    def create(self, req):
        self.created.append(req)
        return self._resp(CreateMessageResponse, not self.fail)

    def reply(self, req):
        self.replied.append(req)
        return self._resp(ReplyMessageResponse, not (self.fail or self.fail_reply))


def _client(fake: _FakeMessages) -> FeishuClient:
    fc = FeishuClient("cli_test", "secret", "feishu")
    fc._client.im.v1.message = fake  # type: ignore[assignment]
    return fc


def test_send_text_plain_records_ids_and_replies_first_chunk_only() -> None:
    fake = _FakeMessages()
    fc = _client(fake)
    long_text = "\n\n".join("riga " * 100 for _ in range(12))  # > 3500 chars
    last = asyncio.run(fc.send_text("oc_1", long_text, reply_to="om_user"))
    assert last is not None
    assert len(fake.replied) == 1 and fake.replied[0].paths["message_id"] == "om_user"
    assert len(fake.created) >= 1
    assert fake.replied[0].body.msg_type == "text"
    assert all(json.loads(r.body.content)["text"] for r in fake.replied + fake.created)
    assert list(fc.sent_message_ids) == [f"om_{i}" for i in range(1, len(fake.created) + 2)]
    assert last == fc.sent_message_ids[-1]


def test_send_text_uses_card_for_markdown() -> None:
    fake = _FakeMessages()
    fc = _client(fake)
    mid = asyncio.run(fc.send_text("oc_1", "## Riepilogo\n- prezzo **ok**"))
    assert mid == "om_1"
    req = fake.created[0]
    assert req.body.msg_type == "interactive"
    assert req.body.receive_id == "oc_1"
    assert req.queries == [("receive_id_type", "chat_id")]
    card = json.loads(req.body.content)
    assert card["schema"] == "2.0"
    assert "**Riepilogo**" in card["body"]["elements"][0]["content"]


def test_send_text_blank_sends_nothing() -> None:
    fake = _FakeMessages()
    fc = _client(fake)
    assert asyncio.run(fc.send_text("oc_1", "   ")) is None
    assert fake.created == [] and fake.replied == []


def test_send_text_failure_returns_none_and_never_raises() -> None:
    fake = _FakeMessages(fail=True)
    fc = _client(fake)
    assert asyncio.run(fc.send_text("oc_1", "ciao")) is None
    assert len(fc.sent_message_ids) == 0


def test_send_falls_back_to_create_when_reply_fails() -> None:
    fake = _FakeMessages(fail_reply=True)
    fc = _client(fake)
    assert asyncio.run(fc.send_text("oc_1", "ciao", reply_to="om_gone")) == "om_2"
    assert len(fake.replied) == 1 and len(fake.created) == 1


def test_send_card_and_unexpected_exception_is_swallowed() -> None:
    fake = _FakeMessages()
    fc = _client(fake)
    assert asyncio.run(fc.send_card("oc_1", {"schema": "2.0"})) == "om_1"
    assert fake.created[0].body.msg_type == "interactive"

    def boom(req):  # network blew up inside the SDK
        raise ConnectionError("offline")

    fc._client.im.v1.message.create = boom  # type: ignore[assignment]
    assert asyncio.run(fc.send_card("oc_1", {"schema": "2.0"})) is None


def test_send_file_docx_falls_back_to_stream_then_sends(tmp_path: Path) -> None:
    fake = _FakeMessages()
    fc = _client(fake)
    path = tmp_path / "nbo.docx"
    path.write_bytes(b"PK\x03\x04 fake docx")
    attempts: list[str] = []

    class _FakeFiles:
        def create(self, req):
            attempts.append(req.body.file_type)
            assert req.body.file_name == "nbo.docx"
            assert not req.body.file.closed  # handle still open during the call
            if req.body.file_type == "doc":
                return CreateFileResponse({"code": 234001, "msg": "invalid file type"})
            return CreateFileResponse({"code": 0, "msg": "ok", "data": {"file_key": "file_ok"}})

    fc._client.im.v1.file = _FakeFiles()  # type: ignore[assignment]
    assert asyncio.run(fc.send_file("oc_1", path)) == "om_1"
    assert attempts == ["doc", "stream"]
    assert fake.created[0].body.msg_type == "file"
    assert json.loads(fake.created[0].body.content) == {"file_key": "file_ok"}


def test_upload_file_rejects_oversize_and_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fc = _client(_FakeMessages())
    empty = tmp_path / "vuoto.pdf"
    empty.write_bytes(b"")
    with pytest.raises(FeishuError):
        asyncio.run(fc.upload_file(empty))
    monkeypatch.setattr(client_mod, "MAX_FILE_BYTES", 10)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * 11)
    with pytest.raises(FeishuError, match="troppo grande"):
        asyncio.run(fc.upload_file(big))
    # send_file wraps the same failure and returns None.
    assert asyncio.run(fc.send_file("oc_1", big)) is None


def test_reactions(tmp_path: Path) -> None:
    fc = _client(_FakeMessages())
    calls: list[tuple[str, str]] = []

    class _FakeReactions:
        def create(self, req):
            calls.append(("create", req.body.reaction_type.emoji_type))
            return CreateMessageReactionResponse({"code": 0, "msg": "ok", "data": {"reaction_id": "re_1"}})

        def delete(self, req):
            calls.append(("delete", req.paths["reaction_id"]))
            return CreateMessageReactionResponse({"code": 0, "msg": "ok"})

    fc._client.im.v1.message_reaction = _FakeReactions()  # type: ignore[assignment]
    assert asyncio.run(fc.add_reaction("om_1")) == "re_1"
    assert asyncio.run(fc.add_reaction("om_1", "DONE")) == "re_1"
    asyncio.run(fc.remove_reaction("om_1", "re_1"))
    asyncio.run(fc.remove_reaction("om_1", ""))  # no-op
    assert calls == [("create", "OnIt"), ("create", "DONE"), ("delete", "re_1")]


def test_download_resource_writes_inbox_file(tmp_path: Path) -> None:
    import io

    from lark_oapi.api.im.v1 import GetMessageResourceResponse

    fc = _client(_FakeMessages())

    class _FakeResources:
        def get(self, req):
            assert req.paths == {"message_id": "om_9", "file_key": "file_k"}
            assert req.queries == [("type", "file")]
            resp = GetMessageResourceResponse()
            resp.code = 0
            resp.file = io.BytesIO(b"%PDF-1.4 fake")
            resp.file_name = "ignored.pdf"
            return resp

    fc._client.im.v1.message_resource = _FakeResources()  # type: ignore[assignment]
    inbox = tmp_path / "inbox" / "oc_1"
    dest = asyncio.run(fc.download_resource("om_9", "file_k", "file", inbox, "Visura (LE).pdf"))
    assert dest.parent == inbox
    assert dest.name.endswith("_Visura_LE.pdf")
    assert len(dest.name.split("_")[0]) == 15  # YYYYMMDD-HHMMSS
    assert dest.read_bytes() == b"%PDF-1.4 fake"

    class _Failing:
        def get(self, req):
            return GetMessageResourceResponse({"code": 230002, "msg": "not in chat"})

    fc._client.im.v1.message_resource = _Failing()  # type: ignore[assignment]
    with pytest.raises(FeishuError, match="230002"):
        asyncio.run(fc.download_resource("om_9", "file_k", "file", inbox, "x.pdf"))


def test_bot_open_id_reads_top_level_bot_key() -> None:
    import lark_oapi as lark

    fc = _client(_FakeMessages())
    seen: list[lark.BaseRequest] = []

    def fake_request(req, option=None):
        seen.append(req)
        resp = lark.BaseResponse({"code": 0, "msg": "ok"})
        resp.raw = lark.RawResponse()
        resp.raw.content = json.dumps({"code": 0, "msg": "ok", "bot": {"open_id": "ou_bot", "app_name": "WiseGlow Desk"}}).encode()
        return resp

    fc._client.request = fake_request  # type: ignore[assignment]
    assert asyncio.run(fc.bot_open_id()) == "ou_bot"
    assert fc.bot_name == "WiseGlow Desk"
    assert seen[0].uri == "/open-apis/bot/v3/info"
    assert seen[0].http_method == lark.HttpMethod.GET
    assert seen[0].token_types == {lark.AccessTokenType.TENANT}
    assert asyncio.run(fc.bot_open_id()) == "ou_bot"  # cached
    assert len(seen) == 1


def test_bot_open_id_returns_none_on_failure() -> None:
    import lark_oapi as lark

    fc = _client(_FakeMessages())
    fc._client.request = lambda req, option=None: lark.BaseResponse({"code": 99991663, "msg": "bad token"})  # type: ignore[assignment]
    assert asyncio.run(fc.bot_open_id()) is None
