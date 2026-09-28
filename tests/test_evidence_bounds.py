import pytest

from app.evidence import EvidenceBlobTooLarge, bounded_chunks


async def chunks(*values: bytes):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_bounded_chunks_allows_exact_limit():
    observed = []
    async for chunk in bounded_chunks(chunks(b"12", b"345"), 5):
        observed.append(chunk)
    assert observed == [b"12", b"345"]


@pytest.mark.asyncio
async def test_bounded_chunks_rejects_chunked_overflow():
    observed = []
    with pytest.raises(EvidenceBlobTooLarge):
        async for chunk in bounded_chunks(chunks(b"12", b"345", b"6"), 5):
            observed.append(chunk)
    assert observed == [b"12", b"345"]
