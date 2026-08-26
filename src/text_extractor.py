"""Extract plain text with page/section markers from PDF or XML files."""
from pathlib import Path


def extract_text(file_path: Path, max_chars: int = 150000) -> tuple[str, str]:
    """Return (text, source_format) where source_format is 'pdf' or 'xml'."""
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(file_path, max_chars), "pdf"
    elif suffix == ".xml":
        return _extract_xml(file_path, max_chars), "xml"
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


def _extract_pdf(file_path: Path, max_chars: int) -> str:
    import fitz  # pymupdf

    doc = fitz.open(str(file_path))
    try:
        parts = []
        total = 0

        for page_num, page in enumerate(doc, start=1):
            page_text = page.get_text("text").strip()
            if not page_text:
                continue

            paragraphs = [p.strip() for p in page_text.split("\n\n") if p.strip()]
            for para_num, para in enumerate(paragraphs, start=1):
                marker = f"[Page {page_num}, Para {para_num}]\n"
                chunk = marker + para + "\n\n"
                if total + len(chunk) > max_chars:
                    parts.append(f"[Truncated at {max_chars} characters]")
                    return "".join(parts)
                parts.append(chunk)
                total += len(chunk)

        return "".join(parts)
    finally:
        # A malformed PDF raising mid-iteration used to skip doc.close()
        # entirely, leaking the MuPDF handle. Curation walks ~10k PDFs in one
        # process, so the leak is not theoretical.
        doc.close()


def _extract_xml(file_path: Path, max_chars: int) -> str:
    from lxml import etree

    tree = etree.parse(str(file_path))
    root = tree.getroot()

    # Strip namespace prefixes for easier XPath
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    parts = []
    total = 0

    # Try article body sections first (JATS XML), fall back to full text walk
    body = root.find(".//body")
    if body is None:
        body = root  # fallback: walk everything

    for section in body.iter("sec"):
        title_elem = section.find("title")
        section_name = title_elem.text.strip() if title_elem is not None and title_elem.text else "Unknown Section"

        paras = section.findall("p")
        for para_num, para_elem in enumerate(paras, start=1):
            para_text = "".join(para_elem.itertext()).strip()
            if not para_text:
                continue
            marker = f"[Section: {section_name}, Para {para_num}]\n"
            chunk = marker + para_text + "\n\n"
            if total + len(chunk) > max_chars:
                parts.append(f"[Truncated at {max_chars} characters]")
                return "".join(parts)
            parts.append(chunk)
            total += len(chunk)

    # If no <sec> elements found, fall back to all <p> elements
    if not parts:
        for para_num, para_elem in enumerate(root.iter("p"), start=1):
            para_text = "".join(para_elem.itertext()).strip()
            if not para_text:
                continue
            marker = f"[Section: Body, Para {para_num}]\n"
            chunk = marker + para_text + "\n\n"
            if total + len(chunk) > max_chars:
                parts.append(f"[Truncated at {max_chars} characters]")
                return "".join(parts)
            parts.append(chunk)
            total += len(chunk)

    return "".join(parts)
