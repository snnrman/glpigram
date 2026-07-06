"""Attachment extraction / size-limit / download helper (feature 6)."""

from __future__ import annotations

import io
import types

from bot.services import attachments

# asyncio_mode=auto (pyproject) runs the one async test without an explicit mark.


def _msg(*, document=None, photo=None, caption=None):
    return types.SimpleNamespace(document=document, photo=photo, caption=caption)


def test_extract_document():
    doc = types.SimpleNamespace(
        file_id="F1",
        file_unique_id="U1",
        file_name="report.pdf",
        file_size=1234,
        mime_type="application/pdf",
    )
    pf = attachments.extract(_msg(document=doc))
    assert pf.file_id == "F1"
    assert pf.filename == "report.pdf"
    assert pf.size == 1234
    assert pf.mime == "application/pdf"


def test_extract_photo_takes_largest():
    small = types.SimpleNamespace(file_id="S", file_unique_id="us", file_size=100)
    large = types.SimpleNamespace(file_id="L", file_unique_id="ul", file_size=9000)
    pf = attachments.extract(_msg(photo=[small, large]))
    assert pf.file_id == "L"
    assert pf.filename.endswith(".jpg")
    assert pf.mime == "image/jpeg"


def test_extract_none_when_no_media():
    assert attachments.extract(_msg()) is None


def test_document_without_name_gets_fallback():
    doc = types.SimpleNamespace(
        file_id="F1", file_unique_id="U1", file_name=None, file_size=0, mime_type=None
    )
    pf = attachments.extract(_msg(document=doc))
    assert pf.filename == "document_U1"
    assert pf.mime == "application/octet-stream"


def test_too_large_boundary():
    at_limit = attachments.PendingFile("f", "f", attachments.MAX_TG_FILE_BYTES, "x")
    over = attachments.PendingFile("f", "f", attachments.MAX_TG_FILE_BYTES + 1, "x")
    unknown = attachments.PendingFile("f", "f", 0, "x")
    assert attachments.too_large(at_limit) is False
    assert attachments.too_large(over) is True
    assert attachments.too_large(unknown) is False  # unknown size is not rejected upfront


async def test_download_reads_bytes():
    class FakeBot:
        async def download(self, file_id):
            assert file_id == "F1"
            return io.BytesIO(b"binary-bytes")

    assert await attachments.download(FakeBot(), "F1") == b"binary-bytes"
