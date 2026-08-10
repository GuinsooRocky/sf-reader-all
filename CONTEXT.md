# Universal Content Reading

sf-reader-all turns heterogeneous inputs into one normalized form for later reading, analysis, and archiving.

## Language

**Source**:
A URL or local document supplied for reading.
_Avoid_: Input, resource

**Content Item**:
The normalized title, body, origin, media metadata, and processing state produced from one Source.
_Avoid_: Article, record

**Inbox**:
The persisted, deduplicated collection of Content Items waiting for downstream processing.
_Avoid_: Cache, database

**Snapshot**:
A self-contained HTML capture that preserves one URL Source for offline viewing.
_Avoid_: Content Item, download

## Relationships

- One **Source** produces at most one **Content Item** per read attempt
- An **Inbox** contains up to 500 **Content Items**
- One URL **Source** may also produce one **Snapshot** during archiving
- A **Snapshot** is an archival artifact and does not replace its **Content Item**

## Example dialogue

> **Dev:** "When a local PDF Source is read, does it go through the web fetchers?"
> **Domain expert:** "No. Its document Adapter produces a Content Item directly, and the Inbox stores it through the same persistence path as URL content."

## Flagged ambiguities

- "read" previously meant both network fetching and normalization; resolved: reading accepts a **Source**, while fetching is only the network part of reading a URL Source.
