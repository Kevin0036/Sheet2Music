import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction

from sheet2music.core.timeline import (
    analyze_measure,
    notated_duration_units,
    units_to_beats,
)


def _note(
    note_type: str | None,
    *,
    dots: int = 0,
    actual_notes: int | None = None,
    normal_notes: int | None = None,
) -> ET.Element:
    note = ET.Element("note")
    if note_type is not None:
        ET.SubElement(note, "type").text = note_type
    for _ in range(dots):
        ET.SubElement(note, "dot")
    if actual_notes is not None and normal_notes is not None:
        modification = ET.SubElement(note, "time-modification")
        ET.SubElement(modification, "actual-notes").text = str(actual_notes)
        ET.SubElement(modification, "normal-notes").text = str(normal_notes)
    return note


class TimelineTest(unittest.TestCase):
    def test_different_divisions_normalize_to_four_beats(self) -> None:
        self.assertEqual(units_to_beats(16, 4), Fraction(4, 1))
        self.assertEqual(units_to_beats(96, 24), Fraction(4, 1))

    def test_chord_and_cross_staff_voice_do_not_create_extra_time(self) -> None:
        measure = ET.fromstring(
            """
            <measure number="1">
              <note>
                <pitch><step>C</step><octave>3</octave></pitch>
                <duration>8</duration><voice>1</voice><staff>2</staff>
              </note>
              <note>
                <chord/><pitch><step>E</step><octave>4</octave></pitch>
                <duration>8</duration><voice>1</voice><staff>1</staff>
              </note>
              <note>
                <pitch><step>G</step><octave>4</octave></pitch>
                <duration>8</duration><voice>1</voice><staff>1</staff>
              </note>
            </measure>
            """
        )

        timeline = analyze_measure(measure, divisions=4, beats=4, beat_type=4)

        self.assertEqual(timeline.final_cursor_units, 16)
        self.assertEqual(timeline.maximum_note_end_units, 16)
        self.assertEqual([event.onset_units for event in timeline.events], [0, 0, 8])
        self.assertFalse(timeline.has_overflow)

    def test_backup_below_zero_is_invalid(self) -> None:
        measure = ET.fromstring(
            """
            <measure number="1">
              <backup><duration>4</duration></backup>
              <note><rest/><duration>4</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            """
        )

        timeline = analyze_measure(measure, divisions=4, beats=4, beat_type=4)

        self.assertEqual(timeline.final_cursor_units, 0)
        self.assertEqual([item.code for item in timeline.diagnostics], ["negative_cursor"])

    def test_grace_note_does_not_advance_cursor(self) -> None:
        measure = ET.fromstring(
            """
            <measure number="1">
              <note><grace/><pitch><step>D</step><octave>4</octave></pitch><voice>1</voice></note>
              <note><pitch><step>E</step><octave>4</octave></pitch><duration>16</duration><voice>1</voice></note>
            </measure>
            """
        )

        timeline = analyze_measure(measure, divisions=4, beats=4, beat_type=4)

        self.assertEqual(timeline.final_cursor_units, 16)
        self.assertEqual([event.duration_units for event in timeline.events], [0, 16])

    def test_tracks_explicit_gap_per_staff_and_voice(self) -> None:
        measure = ET.fromstring(
            """
            <measure number="1">
              <note><pitch><step>C</step><octave>4</octave></pitch>
                <duration>4</duration><voice>1</voice><staff>1</staff></note>
              <forward><duration>8</duration></forward>
              <note><pitch><step>D</step><octave>4</octave></pitch>
                <duration>4</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            """
        )

        timeline = analyze_measure(measure, divisions=4, beats=3, beat_type=4)

        self.assertEqual([event.gap_before_units for event in timeline.events], [0, 8])
        self.assertEqual(timeline.events[1].gap_source, "forward")
        self.assertTrue(timeline.has_overflow)

    def test_notated_duration_handles_dots_and_triplets(self) -> None:
        self.assertEqual(notated_duration_units(_note("eighth", dots=1), 24), 18)
        self.assertEqual(notated_duration_units(_note("quarter", dots=2), 16), 28)
        self.assertEqual(
            notated_duration_units(
                _note("eighth", actual_notes=3, normal_notes=2),
                24,
            ),
            8,
        )

    def test_notated_duration_requires_complete_integral_evidence(self) -> None:
        self.assertIsNone(notated_duration_units(_note(None), 24))
        self.assertIsNone(notated_duration_units(_note("1024th"), 3))
        self.assertIsNone(
            notated_duration_units(
                _note("eighth", actual_notes=3, normal_notes=2),
                4,
            )
        )


if __name__ == "__main__":
    unittest.main()
