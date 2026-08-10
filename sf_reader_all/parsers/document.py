"""Convert local office documents to :class:`UnifiedContent`."""

from os import PathLike
from pathlib import Path

from sf_reader_all.schema import SourceType, UnifiedContent


def read_document(path: str | PathLike[str]) -> UnifiedContent:
    """Read a local document through the optional ``firecrawl-anydoc`` package."""
    document_path = Path(path).expanduser().resolve()
    if not document_path.is_file():
        raise FileNotFoundError(f"Document not found: {document_path}")

    try:
        import anydoc
    except ImportError as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == "anydoc":
            raise RuntimeError(
                "Document support is optional. Install `firecrawl-anydoc` in the "
                "same environment, or install this project with `.[documents]`."
            ) from exc
        raise

    markdown = anydoc.to_markdown(str(document_path))
    return UnifiedContent(
        source_type=SourceType.DOCUMENT,
        source_name=document_path.name,
        title=document_path.stem,
        content=markdown,
        url=document_path.as_uri(),
        extra={
            "filename": document_path.name,
            "suffix": document_path.suffix.lower(),
            "size_bytes": document_path.stat().st_size,
        },
    )
