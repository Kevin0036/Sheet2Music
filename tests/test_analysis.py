import unittest
import xml.etree.ElementTree as ET

from sheet2music.core.analysis import analyze_musicxml_tree
from sheet2music.core.structure import ScoreStructurePlan


def _measure(number: int, *, signature: str | None = None, duration: int = 16) -> ET.Element:
    measure = ET.Element("measure", number=str(number))
    attributes = ET.SubElement(measure, "attributes")
    ET.SubElement(attributes, "divisions").text = "4"
    if signature is not None:
        beats, beat_type = signature.split("/")
        time = ET.SubElement(attributes, "time")
        ET.SubElement(time, "beats").text = beats
        ET.SubElement(time, "beat-type").text = beat_type
    note = ET.SubElement(measure, "note")
    ET.SubElement(note, "rest")
    ET.SubElement(note, "duration").text = str(duration)
    ET.SubElement(note, "voice").text = "1"
    ET.SubElement(note, "staff").text = "1"
    return measure


def _score_with_part(*measures: ET.Element) -> ET.Element:
    root = ET.Element("score-partwise", version="4.0")
    part = ET.SubElement(root, "part", id="P1")
    for measure in measures:
        part.append(measure)
    return root


def _add_clef(measure: ET.Element, *, staff: int, sign: str, line: int) -> None:
    attributes = measure.find("attributes")
    assert attributes is not None
    clef = ET.SubElement(attributes, "clef", number=str(staff))
    ET.SubElement(clef, "sign").text = sign
    ET.SubElement(clef, "line").text = str(line)


class AnalysisTest(unittest.TestCase):
    def test_reports_isolated_time_signature_change_instead_of_filtering_it(self) -> None:
        root = _score_with_part(
            _measure(1, signature="4/4"),
            _measure(2, signature="2/4", duration=8),
            _measure(3, signature="4/4"),
        )
        plan = ScoreStructurePlan.from_dict({})

        report = analyze_musicxml_tree(root, plan, page_measure_offsets=[0, 2])

        findings = [item for item in report.findings if item.kind == "time_signature_change"]
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0].measure_start, 2)
        self.assertEqual(findings[0].measure_end, 2)
        self.assertEqual(findings[0].observed["signature"], "2/4")
        self.assertEqual(findings[0].suggestion["signature"], "4/4")
        self.assertEqual(findings[0].page_numbers, [1])
        self.assertEqual(findings[0].severity, "high")

    def test_does_not_repeat_already_confirmed_time_signature_change(self) -> None:
        root = _score_with_part(
            _measure(1, signature="4/4"),
            _measure(2, signature="2/4", duration=8),
            _measure(3, signature="4/4"),
        )
        plan = ScoreStructurePlan.from_dict(
            {
                "time_signature_changes": [
                    {"from_measure": 2, "to_measure": 2, "signature": "2/4"},
                    {"from_measure": 3, "signature": "4/4"},
                ]
            }
        )

        report = analyze_musicxml_tree(root, plan)

        self.assertFalse(
            [item for item in report.findings if item.kind == "time_signature_change"]
        )

    def test_reports_conflicting_part_signatures(self) -> None:
        root = ET.Element("score-partwise", version="4.0")
        for part_id, signature in (("P1", "4/4"), ("P2", "3/4")):
            part = ET.SubElement(root, "part", id=part_id)
            part.append(_measure(1, signature=signature, duration=16 if signature == "4/4" else 12))

        report = analyze_musicxml_tree(root, ScoreStructurePlan.from_dict({}))

        findings = [item for item in report.findings if item.kind == "conflicting_time_signature"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].measure_start, 1)
        self.assertEqual(findings[0].severity, "high")

    def test_reports_measure_overflow_in_normalized_beats(self) -> None:
        root = ET.Element("score-partwise", version="4.0")
        part = ET.SubElement(root, "part", id="P1")
        measure = _measure(1, signature="4/4", duration=20)
        part.append(measure)

        report = analyze_musicxml_tree(root, ScoreStructurePlan.from_dict({}))

        findings = [item for item in report.findings if item.kind == "timing_measure_overflow"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")
        self.assertEqual(findings[0].observed["occupied_units"], 20)
        self.assertEqual(findings[0].observed["expected_units"], 16)
        self.assertEqual(findings[0].observed["occupied_beats"], "5")
        self.assertEqual(findings[0].observed["expected_beats"], "4")
        self.assertEqual(findings[0].observed["difference_beats"], "1")
        self.assertEqual(findings[0].available_actions, ["reidentify"])
        self.assertEqual(findings[0].to_dict()["part_id"], "P1")

    def test_underfilled_measure_does_not_require_review(self) -> None:
        report = analyze_musicxml_tree(
            _score_with_part(_measure(1, signature="4/4", duration=8)),
            ScoreStructurePlan.from_dict({}),
        )

        self.assertFalse(report.requires_review)
        self.assertFalse([item for item in report.findings if item.kind.startswith("timing_")])

    def test_overflow_offers_automatic_repair_only_with_complete_evidence(self) -> None:
        measure = _measure(1, signature="4/4", duration=20)
        note = measure.find("note")
        assert note is not None
        ET.SubElement(note, "type").text = "whole"

        report = analyze_musicxml_tree(
            _score_with_part(measure),
            ScoreStructurePlan.from_dict({}),
        )

        finding = next(
            item for item in report.findings if item.kind == "timing_measure_overflow"
        )
        self.assertEqual(finding.available_actions, ["correct", "reidentify"])
        self.assertEqual(finding.suggestion["action"], "compress")
        self.assertEqual(finding.suggestion["corrected_note_count"], 1)
        self.assertEqual(finding.suggestion["resulting_beats"], "4")

    def test_negative_cursor_is_a_specific_timing_risk(self) -> None:
        measure = _measure(1, signature="4/4", duration=16)
        measure.insert(1, ET.fromstring("<backup><duration>4</duration></backup>"))

        report = analyze_musicxml_tree(
            _score_with_part(measure),
            ScoreStructurePlan.from_dict({}),
        )

        findings = [item for item in report.findings if item.kind == "timing_cursor_invalid"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].available_actions, ["reidentify"])

    def test_clef_finding_keeps_staff_for_review_decision(self) -> None:
        first = _measure(1)
        second = _measure(2)
        _add_clef(first, staff=2, sign="G", line=2)
        _add_clef(second, staff=2, sign="F", line=4)

        report = analyze_musicxml_tree(
            _score_with_part(first, second),
            ScoreStructurePlan.from_dict({}),
        )

        finding = next(
            item for item in report.findings if item.kind == "clef_change_at_measure_start"
        )
        self.assertEqual(finding.to_dict()["staff"], 2)
        self.assertEqual(finding.to_dict()["offset_units"], 0)
        self.assertEqual(finding.to_dict()["offset_beats"], "0")
        self.assertEqual(finding.to_dict()["hand_region"], "left")
        self.assertEqual(finding.available_actions, ["preserve", "reidentify"])

    def test_mid_measure_clef_change_keeps_exact_beat_position(self) -> None:
        first = _measure(1)
        _add_clef(first, staff=2, sign="F", line=4)
        second = _measure(2)
        attributes = second.find("attributes")
        note = second.find("note")
        assert attributes is not None and note is not None
        second.remove(note)
        first_half = ET.fromstring(
            "<note><rest/><duration>8</duration><voice>1</voice><staff>2</staff></note>"
        )
        second.append(first_half)
        mid_attributes = ET.SubElement(second, "attributes")
        clef = ET.SubElement(mid_attributes, "clef", number="2")
        ET.SubElement(clef, "sign").text = "G"
        ET.SubElement(clef, "line").text = "2"
        second.append(note)
        note.find("duration").text = "8"
        note.find("staff").text = "2"

        report = analyze_musicxml_tree(
            _score_with_part(first, second),
            ScoreStructurePlan.from_dict({}),
        )

        finding = next(
            item for item in report.findings if item.kind == "clef_change_mid_measure"
        )
        payload = finding.to_dict()
        self.assertEqual(payload["staff"], 2)
        self.assertEqual(payload["measure_start"], 2)
        self.assertEqual(payload["offset_units"], 8)
        self.assertEqual(payload["offset_beats"], "2")
        self.assertEqual(payload["observed"]["previous_clef"], {"sign": "F", "line": 4})
        self.assertEqual(payload["observed"]["observed_clef"], {"sign": "G", "line": 2})


if __name__ == "__main__":
    unittest.main()
