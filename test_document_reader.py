"""Tests for the optional local document reader."""

import asyncio
import builtins
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

from sf_reader_all.reader import UniversalReader
from sf_reader_all.schema import SourceType, UnifiedInbox


def _fake_anydoc(markdown: str = "# Converted\n\nDocument body"):
    return types.SimpleNamespace(to_markdown=lambda path: markdown)


def test_read_document_normalizes_local_file():
    from sf_reader_all.parsers.document import read_document

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "quarterly-report.docx"
        path.write_bytes(b"fake docx bytes")

        with patch.dict(sys.modules, {"anydoc": _fake_anydoc()}):
            item = read_document(path)

    assert item.source_type == SourceType.DOCUMENT
    assert item.source_name == "quarterly-report.docx"
    assert item.title == "quarterly-report"
    assert item.content == "# Converted\n\nDocument body"
    assert item.url.startswith("file://")
    assert item.extra["suffix"] == ".docx"
    assert item.extra["size_bytes"] == len(b"fake docx bytes")


def test_universal_reader_routes_existing_file_and_saves_it():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "notes.pdf"
        path.write_bytes(b"fake pdf bytes")
        inbox_path = Path(directory) / "inbox.json"
        reader = UniversalReader(inbox=UnifiedInbox(str(inbox_path)))

        with patch.dict(sys.modules, {"anydoc": _fake_anydoc("# Notes")}):
            item = asyncio.run(reader.read_source(str(path)))

        saved = UnifiedInbox(str(inbox_path))

        assert item.source_type == SourceType.DOCUMENT
        assert item.content == "# Notes"
        assert len(saved.items) == 1
        assert saved.items[0].url == path.resolve().as_uri()


def test_url_only_entry_does_not_read_local_files():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "private.docx"
        path.write_bytes(b"local-only")

        try:
            asyncio.run(UniversalReader().read(str(path)))
        except ValueError as error:
            assert "hostname" in str(error).lower()
        else:
            raise AssertionError("URL-only reader unexpectedly accepted a local file")


def test_anydoc_conversion_does_not_block_event_loop():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "slow.pdf"
        path.write_bytes(b"fake pdf bytes")

        def slow_conversion(_path):
            time.sleep(0.08)
            return "# Converted off-loop"

        async def scenario():
            reader = UniversalReader()
            task = asyncio.create_task(reader.read_file(path))
            await asyncio.sleep(0.01)
            assert not task.done(), "anydoc conversion blocked the event loop"
            return await task

        with (
            patch.dict(sys.modules, {"anydoc": _fake_anydoc()}),
            patch("anydoc.to_markdown", side_effect=slow_conversion),
            patch("sf_reader_all.utils.storage.save_many_to_markdown"),
        ):
            item = asyncio.run(scenario())

    assert item.content == "# Converted off-loop"


def test_missing_anydoc_gets_install_hint_but_internal_import_error_survives():
    from sf_reader_all.parsers.document import read_document

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fixture.docx"
        path.write_bytes(b"fixture")
        original_import = builtins.__import__

        def missing_anydoc(name, *args, **kwargs):
            if name == "anydoc":
                raise ModuleNotFoundError(
                    "No module named 'anydoc'", name="anydoc"
                )
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_anydoc):
            try:
                read_document(path)
            except RuntimeError as error:
                assert "firecrawl-anydoc" in str(error)
            else:
                raise AssertionError("missing anydoc did not show install guidance")

        def broken_anydoc_dependency(name, *args, **kwargs):
            if name == "anydoc":
                raise ModuleNotFoundError(
                    "No module named 'native_dependency'",
                    name="native_dependency",
                )
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=broken_anydoc_dependency):
            try:
                read_document(path)
            except ModuleNotFoundError as error:
                assert error.name == "native_dependency"
            else:
                raise AssertionError("anydoc's internal import error was hidden")


if __name__ == "__main__":
    test_read_document_normalizes_local_file()
    print("PASS: local document is normalized")
    test_universal_reader_routes_existing_file_and_saves_it()
    print("PASS: UniversalReader routes and saves local documents")
    test_url_only_entry_does_not_read_local_files()
    print("PASS: URL-only reader does not expose local files")
    test_anydoc_conversion_does_not_block_event_loop()
    print("PASS: anydoc conversion stays off the event loop")
    test_missing_anydoc_gets_install_hint_but_internal_import_error_survives()
    print("PASS: anydoc import errors preserve their real cause")
    print("ALL PASS")
