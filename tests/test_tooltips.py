"""Every label on the page has an explanation, and every explanation has a label.

The page attaches the explanations in dashboard.js's GLOSSARY to labels by
their text (or by data-tip-key). A new label without an entry, or an entry
whose label was renamed, would silently show nothing.
"""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "dashboard" / "static" / "index.html"
SCRIPT = ROOT / "dashboard" / "static" / "dashboard.js"

# Kept in step with attachTooltips() in dashboard.js.
LABEL_CLASSES = {"stat-key", "card-title", "tile-label", "history-title", "lbl"}
LABEL_TAGS = {"th", "dt"}


class Labels(HTMLParser):
    """Texts of the elements attachTooltips() looks at."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.labels = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        key = attrs.get("data-tip-key")
        wanted = bool(classes & LABEL_CLASSES) or tag in LABEL_TAGS or (
            tag == "span" and "title" in classes)
        self.stack.append([tag, wanted, key, ""])

    def handle_endtag(self, tag):
        while self.stack:
            name, wanted, key, text = self.stack.pop()
            if key:
                self.labels.add(key)
            elif wanted:
                label = re.sub(r"^[^A-Za-z0-9]+", "", re.sub(r"\s+", " ", text).strip())
                if label:
                    self.labels.add(label)
            if self.stack:
                self.stack[-1][3] += text
            if name == tag:
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1][3] += data


def glossary_keys():
    script = SCRIPT.read_text()
    start = script.index("const GLOSSARY = {")
    block = script[start:script.index("\n};", start)]
    single = re.findall(r"^\s*'((?:[^'\\]|\\.)+)'\s*:", block, re.M)
    double = re.findall(r'^\s*"([^"]+)"\s*:', block, re.M)
    return {k.replace("\\'", "'") for k in single + double}


class TooltipTests(unittest.TestCase):
    def setUp(self):
        parser = Labels()
        parser.feed(PAGE.read_text())
        self.labels = parser.labels
        self.keys = glossary_keys()

    def test_every_label_is_explained(self):
        self.assertEqual(sorted(self.labels - self.keys), [])

    def test_every_explanation_has_a_label(self):
        # Table headers built by the script are listed here too.
        self.assertEqual(sorted(self.keys - self.labels), [])


if __name__ == "__main__":
    unittest.main()
