from io import BytesIO
from typing import Any, BinaryIO, Optional, Union

from lxml import etree

XmlSource = Union[bytes, str, BinaryIO]


def _fold(
    attrib: dict[str, str], children: list[tuple[str, Any]], text: str
) -> Optional[Union[str, dict[str, Any]]]:
    """Combine an element's attributes, own text, and already-converted children into one value.
    :param attrib: the element's attributes
    :param children: (tag, value) pairs for each child
    :param text: the element's own stripped text
    :return: None, str, dict
    """
    value: dict[str, Any] = {f"@{name}": val for name, val in attrib.items()}
    for tag, child_value in children:
        if tag in value:
            existing = value[tag]
            if isinstance(existing, list):
                existing.append(child_value)
            else:
                value[tag] = [existing, child_value]
        else:
            value[tag] = child_value

    if not value:
        return text or None
    if text:
        value["#text"] = text
    return value


def parse_xml(xml_source: XmlSource) -> dict[str, Any]:
    """Convert an XML document into a JSON-compatible dict via lxml iterparse.
    :param xml_source: raw XML document as bytes, str, or a binary file-like object (e.g. an open dump file)
    :return: dict with a single key, the document's root tag
    """
    if isinstance(xml_source, str):
        xml_source = xml_source.encode("utf-8")
    if isinstance(xml_source, bytes):
        xml_source = BytesIO(xml_source)

    stack: list[dict[str, Any]] = []
    result: Optional[dict[str, Any]] = None

    context = etree.iterparse(xml_source, events=("start", "end"))
    for event, elem in context:
        if event == "start":
            stack.append(
                {
                    "tag": elem.tag,
                    "attrib": dict(elem.attrib),
                    "children": [],
                    "tails": [],
                }
            )
            continue

        frame = stack.pop()
        own_text = (elem.text or "").strip()
        text = " ".join(part for part in (own_text, *frame["tails"]) if part)
        value = _fold(frame["attrib"], frame["children"], text)
        tail = (elem.tail or "").strip()

        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]

        if stack:
            stack[-1]["children"].append((frame["tag"], value))
            if tail:
                stack[-1]["tails"].append(tail)
        else:
            result = {frame["tag"]: value}
    del context

    return result
