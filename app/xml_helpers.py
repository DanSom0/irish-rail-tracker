"""Namespace-tolerant helpers for Irish Rail's XML responses."""

from xml.etree import ElementTree


def local_name(tag: str) -> str:
    """Return a tag without its ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def child_text(element: ElementTree.Element, name: str) -> str:
    """Return a child value regardless of the document's XML namespace."""
    for child in element:
        if local_name(child.tag) == name:
            return child.text.strip() if child.text else ""
    return ""


def has_child(element: ElementTree.Element, name: str) -> bool:
    """Whether a record includes a field at all, even an empty one."""
    return any(local_name(child.tag) == name for child in element)


def find_rows(root: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    """Find record rows in namespaced and non-namespaced responses."""
    namespace = root.tag.partition("}")[0].removeprefix("{") if root.tag.startswith("{") else ""
    path = f".//{{{namespace}}}{name}" if namespace else f".//{name}"
    return root.findall(path)
