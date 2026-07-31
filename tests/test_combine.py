import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from sheet2music.core.combine import combine_page_musicxml

PAGE_XML = """
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="7"><attributes><divisions>4</divisions></attributes><note><rest/><duration>4</duration><voice>1</voice><staff>1</staff></note></measure>
    <measure number="8"><note><rest/><duration>4</duration><voice>1</voice><staff>1</staff></note></measure>
  </part>
</score-partwise>
"""


def _write(tmp: Path, name: str, xml: str) -> Path:
    path = tmp / name
    path.write_text(xml, encoding="utf-8")
    return path


class CombinePageMusicXmlTest(unittest.TestCase):
    def test_combines_and_renumbers_measures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            page1 = _write(tmp, "page-1.musicxml", PAGE_XML)
            page2 = _write(tmp, "page-2.musicxml", PAGE_XML)
            output = tmp / "combined.musicxml"

            stats = combine_page_musicxml([page1, page2], output)

            self.assertEqual(stats, {"num_parts": 1, "num_measures": 4})

            root = ET.parse(output).getroot()
            measures = root.find("./part").findall("measure")
            self.assertEqual([m.get("number") for m in measures], ["1", "2", "3", "4"])

    def test_raises_on_empty_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                combine_page_musicxml([], Path(temp_dir) / "x.musicxml")

    def test_single_page_renumbers_from_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            page = _write(tmp, "page-1.musicxml", PAGE_XML)
            output = tmp / "combined.musicxml"

            combine_page_musicxml([page], output)
            root = ET.parse(output).getroot()
            measures = root.find("./part").findall("measure")
            self.assertEqual([m.get("number") for m in measures], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
