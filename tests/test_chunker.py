from agent.chunker import _tokens, chunk_markdown


def _long_md() -> str:
    para = ("The quick brown fox jumps over the lazy dog and keeps running. " * 20).strip()
    return "\n\n".join(
        ["# Guide", para, para, "## Section One", para, para, para, "## Section Two", para]
    )


def test_chunks_respect_target_size():
    chunks = chunk_markdown(_long_md(), target_tokens=300, overlap_tokens=45)
    assert len(chunks) > 2
    for c in chunks:
        assert _tokens(c.text) <= int(300 * 1.2)


def test_section_metadata_preserved():
    chunks = chunk_markdown(_long_md(), target_tokens=300, overlap_tokens=45)
    sections = {c.section for c in chunks}
    assert "Section One" in sections and "Section Two" in sections


def test_overlap_carries_content():
    chunks = chunk_markdown(_long_md(), target_tokens=300, overlap_tokens=200)
    same_section = [c for c in chunks if c.section == "Section One"]
    assert len(same_section) >= 2
    # the tail of one chunk reappears at the head of the next
    assert same_section[1].text.split("\n\n")[0] in same_section[0].text


def test_giant_paragraph_hard_split():
    huge = "word " * 3000
    chunks = chunk_markdown(huge, target_tokens=400, overlap_tokens=50)
    assert len(chunks) > 1
    for c in chunks:
        assert _tokens(c.text) <= 400


def test_empty_input():
    assert chunk_markdown("") == []
