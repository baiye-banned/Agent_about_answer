"""PDF 文本抽取路径的回归测试（issue #98）。

背景：`backend/crud/knowledge_file.py` 的 `extract_pdf_text` 此前没有任何真实解析用例——
`test_knowledge_service.py` 把 `extract_file_text` 打了桩，`test_upload_validation.py` 只校验扩展名，
因此「PDF 解析真的能从字节里抽出文本吗」这条断言缺位。pypdf 4 → 6 跨两个大版本升级前，
先把这条路径钉住：一旦解析行为或所用 API 变化，下面的用例必须失败。

夹具不依赖任何 PDF 写库（环境里除 pypdf 外没有 reportlab/fpdf），由本文件按字节直接构造，
xref 偏移量按实际位置计算，因此对 pypdf 版本中立：

- `_minimal_pdf` 用内置 Helvetica 画 ASCII 文本，覆盖常规抽取路径；
- `_cjk_pdf` 用 Identity-H 造一个带 ToUnicode 的 CID 字体，覆盖本仓库真正的业务场景
  （上传的 PDF 多为中文），因为中文抽取走的是 CMap 而非 WinAnsi 编码，是另一条分支。
"""

from io import BytesIO

import pytest
from fastapi import HTTPException

from crud.knowledge_file import extract_file_text, extract_pdf_text


# ---------------------------------------------------------------------------
# 夹具构造
# ---------------------------------------------------------------------------


def _serialize_pdf(objects: list[tuple[int, bytes]]) -> bytes:
    """把 (对象号, 对象体) 列表拼成完整 PDF 字节，xref 偏移按实际写入位置计算。"""
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"

    total_objs = len(objects) + 1  # +1 是固定的 0 号空闲对象
    xref_offset = len(out)
    # 每个 xref 表项必须正好 20 字节：10 位偏移 + 空格 + 5 位世代号 + 空格 + 类型 + 空格 + 换行。
    out += b"xref\n0 %d\n" % total_objs
    out += b"0000000000 65535 f \n"
    for num in range(1, total_objs):
        out += b"%010d 00000 n \n" % offsets[num]

    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % total_objs
    out += b"startxref\n%d\n%%%%EOF\n" % xref_offset
    return bytes(out)


def _escape_pdf_text(text: str) -> bytes:
    """转义 PDF 字面量字符串里的 \\ ( )，再按 latin-1 编码成字节。"""
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1")


def _minimal_pdf(page_texts: list[str]) -> bytes:
    """最小可用 PDF：1=Catalog、2=Pages、3=Font(Helvetica)，之后每页占 Page、Contents 两个对象。"""
    page_count = len(page_texts)
    page_obj_nums = [4 + index * 2 for index in range(page_count)]
    content_obj_nums = [num + 1 for num in page_obj_nums]
    kids = b" ".join(b"%d 0 R" % num for num in page_obj_nums)

    objects: list[tuple[int, bytes]] = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % page_count),
        # /Encoding 必须显式声明：简单字体的字节到字符映射由它决定，不写就是不明确的。
        # pypdf 4 对缺失声明按 Latin-1 兜底，pypdf 6 改按 PDF 规范默认的 StandardEncoding，
        # 于是 0xE9(é)/0xFC(ü) 会被解成别的字符。真实 PDF（Word/LibreOffice 等产出）
        # 要么显式声明 WinAnsi，要么用 CID 字体，不会落到这条兜底路径上。
        (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"),
    ]

    for index, text in enumerate(page_texts):
        # 空文本不画任何字形，用来模拟空白页。
        stream = b"BT ET" if not text else (
            b"BT /F1 12 Tf 72 720 Td (" + _escape_pdf_text(text) + b") Tj ET"
        )
        objects.append((
            page_obj_nums[index],
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            b"/Contents %d 0 R >>" % content_obj_nums[index],
        ))
        objects.append((
            content_obj_nums[index],
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        ))

    return _serialize_pdf(objects)


def _to_unicode_cmap(cid_chars: list[tuple[int, str]]) -> bytes:
    """Identity-H 用的 ToUnicode CMap：把 CID 映射回 Unicode 码点。"""
    entries = "".join(
        "<%04X> <%s>\n" % (cid, "".join("%04X" % ord(ch) for ch in text))
        for cid, text in cid_chars
    )
    body = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        "%d beginbfchar\n%s endbfchar\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\nend\n" % (len(cid_chars), entries)
    )
    return body.encode("latin-1")


def _cjk_pdf(page_texts: list[str]) -> bytes:
    """带 ToUnicode 的中文 PDF：Type0 + Identity-H，正文按 CID 编码。

    CID 从 1 起顺序分配（0 保留），每个字符一个 CID，逐字映射回 Unicode。
    对象号：1=Catalog、2=Pages、3=Type0、4=CIDFont、5=Descriptor、6=ToUnicode、7+=每页一个 Contents。
    """
    cid_of: dict[int, list[tuple[int, str]]] = {}
    page_hex: list[str] = []
    next_cid = 1

    for text in page_texts:
        pairs: list[tuple[int, str]] = []
        for char in text:
            pairs.append((next_cid, char))
            next_cid += 1
        cid_of[id(text)] = pairs
        # 所有 CID 必须拼在同一对尖括号内：拆成多个 <...><...> 会被当成多个
        # 字符串对象，Tj 只消费第一个，会静默丢掉后面的字。
        page_hex.append("<%s>" % "".join("%04X" % cid for cid, _ in pairs))

    all_pairs = [pair for text in page_texts for pair in cid_of[id(text)]]
    cmap = _to_unicode_cmap(all_pairs)

    content_obj_nums = [7 + index for index in range(len(page_texts))]
    page_obj_nums = [7 + len(page_texts) + index for index in range(len(page_texts))]
    kids = b" ".join(b"%d 0 R" % num for num in page_obj_nums)

    objects: list[tuple[int, bytes]] = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_texts)),
        (3, b"<< /Type /Font /Subtype /Type0 /BaseFont /Helvetica-Identity-H "
            b"/Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 6 0 R >>"),
        (4, b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Helvetica-Identity-H "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor 5 0 R /DW 1000 /CIDToGIDMap /Identity >>"),
        (5, b"<< /Type /FontDescriptor /FontName /Helvetica-Identity-H /Flags 4 "
            b"/FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 1000 /Descent 0 "
            b"/CapHeight 1000 /StemV 80 >>"),
        (6, b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream"),
    ]

    for index, text in enumerate(page_texts):
        stream = b"BT ET" if not text else (
            b"BT /F1 12 Tf 72 720 Td " + page_hex[index].encode("latin-1") + b" Tj ET"
        )
        objects.append((
            content_obj_nums[index],
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        ))
        objects.append((
            page_obj_nums[index],
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            b"/Contents %d 0 R >>" % content_obj_nums[index],
        ))

    return _serialize_pdf(objects)


# ---------------------------------------------------------------------------
# 夹具自检：先证明夹具本身是「真 PDF」，否则下面的用例会因夹具写错而假绿
# ---------------------------------------------------------------------------


def test_fixture_really_is_a_pdf_with_pages():
    from pypdf import PdfReader

    assert len(PdfReader(BytesIO(_minimal_pdf(["hello"])), strict=False).pages) == 1
    assert len(PdfReader(BytesIO(_cjk_pdf(["你好"])), strict=False).pages) == 1


# ---------------------------------------------------------------------------
# extract_pdf_text — ASCII 路径
# ---------------------------------------------------------------------------


def test_extract_pdf_text_reads_single_page_text():
    result = extract_pdf_text(_minimal_pdf(["Knowledge base regression"]))

    assert "第 1 页" in result
    assert "Knowledge base regression" in result


def test_extract_pdf_text_reads_every_page_in_order():
    result = extract_pdf_text(_minimal_pdf(["alpha page", "beta page", "gamma page"]))

    assert [line for line in result.splitlines() if line.startswith("第 ")] == [
        "第 1 页",
        "第 2 页",
        "第 3 页",
    ]
    assert result.index("alpha page") < result.index("beta page") < result.index("gamma page")


def test_extract_pdf_text_skips_pages_without_text():
    """空白页不应产出「第 N 页」小节，否则预览里会多出空段落。"""
    result = extract_pdf_text(_minimal_pdf(["only page", "", "third page"]))

    assert "第 1 页" in result
    assert "第 2 页" not in result
    assert "第 3 页" in result
    assert "third page" in result


def test_extract_pdf_text_keeps_non_ascii_latin_text():
    result = extract_pdf_text(_minimal_pdf(["café résumé"]))

    assert "café résumé" in result


def test_extract_pdf_text_returns_empty_string_for_pdf_without_pages():
    assert extract_pdf_text(_minimal_pdf([])) == ""


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not a pdf at all",
        b"%PDF-1.4\nthis body is truncated before any object\n",
    ],
)
def test_extract_pdf_text_rejects_unparsable_bytes(content):
    """损坏输入必须转成 400，而不是把解析器异常直接漏给调用方。"""
    with pytest.raises(HTTPException) as exc_info:
        extract_pdf_text(content)

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# extract_pdf_text — 中文（CID / ToUnicode）路径：本仓库上传 PDF 的主要形态
# ---------------------------------------------------------------------------


def test_extract_pdf_text_reads_chinese_text_through_cmap():
    result = extract_pdf_text(_cjk_pdf(["第一章 总则\n本制度自发布之日起施行。"]))

    assert "第一章 总则" in result
    assert "本制度自发布之日起施行。" in result


def test_extract_pdf_text_reads_chinese_from_every_page():
    result = extract_pdf_text(_cjk_pdf(["甲方义务", "乙方义务"]))

    assert "第 1 页" in result and "第 2 页" in result
    assert result.index("甲方义务") < result.index("乙方义务")


def test_extract_pdf_text_strips_blank_chinese_page():
    result = extract_pdf_text(_cjk_pdf(["有效条款", ""]))

    assert "有效条款" in result
    assert "第 2 页" not in result


# ---------------------------------------------------------------------------
# extract_file_text 路由
# ---------------------------------------------------------------------------


def test_extract_file_text_routes_pdf_bytes_to_pdf_extractor():
    result = extract_file_text("制度汇编.PDF", _minimal_pdf(["routed through extension"]))

    assert "routed through extension" in result


def test_extract_file_text_rejects_unparsable_pdf_with_400():
    with pytest.raises(HTTPException) as exc_info:
        extract_file_text("broken.pdf", b"not a pdf at all")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# 与分块链路的衔接：抽出的文本要能被 chunk_text 正常切分且不丢正文
# ---------------------------------------------------------------------------


def test_pdf_text_feeds_chunk_text_without_losing_content():
    """ASCII 正文 + 抽取器自加的中文页码标记，验证页码标记被识别为页边界。"""
    from crud.knowledge_file import _is_page_heading, chunk_text

    text = extract_pdf_text(_minimal_pdf(["Chapter One General", "Effective on release."]))
    chunks = chunk_text(text, file_id=1)

    assert _is_page_heading("第 1 页")
    assert chunks
    assert "Effective on release." in "\n".join(chunk["text"] for chunk in chunks)


def test_chinese_pdf_text_keeps_body_and_headings_after_chunking():
    """中文 PDF 走完整条链路：抽取 → 分块，标题入层级、正文不丢。"""
    from crud.knowledge_file import chunk_text

    text = extract_pdf_text(_cjk_pdf(["第一章 总则", "本制度自发布之日起施行。"]))
    chunks = chunk_text(text, file_id=1)
    joined = "\n".join(chunk["text"] for chunk in chunks)

    assert "本制度自发布之日起施行。" in joined
    assert "第一章 总则" in joined
