import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import miditoolkit

from sheet2music.core import repair


def _build_sample_score() -> ET.ElementTree:
    xml = """
    <score-partwise version="4.0">
      <part-list>
        <score-part id="P1">
          <part-name>Piano</part-name>
        </score-part>
      </part-list>
      <part id="P1">
        <measure number="1">
          <attributes>
            <divisions>4</divisions>
            <staves>2</staves>
            <time><beats>4</beats><beat-type>4</beat-type></time>
          </attributes>
          <attributes>
            <clef number="1"><sign>G</sign><line>2</line></clef>
            <clef number="2"><sign>F</sign><line>4</line></clef>
            <time><beats>4</beats><beat-type>4</beat-type></time>
          </attributes>
          <note>
            <rest />
            <duration>2</duration>
            <voice>1</voice>
            <staff>1</staff>
          </note>
          <backup><duration>2</duration></backup>
          <note>
            <pitch><step>A</step><octave>2</octave></pitch>
            <duration>1</duration>
            <voice>5</voice>
            <staff>2</staff>
          </note>
          <note>
            <pitch><step>E</step><octave>3</octave></pitch>
            <duration>15</duration>
            <voice>5</voice>
            <staff>2</staff>
          </note>
          <note>
            <pitch><step>E</step><octave>4</octave></pitch>
            <duration>14</duration>
            <voice>1</voice>
            <staff>1</staff>
          </note>
        </measure>
      </part>
    </score-partwise>
    """
    return ET.ElementTree(ET.fromstring(xml))


class RepairMusicXmlTest(unittest.TestCase):
    def test_deterministic_timing_repair_changes_only_provable_duration(self) -> None:
        measure = ET.fromstring(
            """
            <measure number="1">
              <attributes><divisions>4</divisions></attributes>
              <note>
                <pitch><step>C</step><octave>4</octave></pitch>
                <duration>20</duration><voice>1</voice><type>whole</type><staff>1</staff>
              </note>
            </measure>
            """
        )

        result = repair.preview_deterministic_timing_repair(measure, 4, 4, 4)

        self.assertTrue(result.applied)
        self.assertEqual(result.corrections, ((0, 20, 16),))
        self.assertIsNotNone(result.measure)
        assert result.measure is not None
        self.assertEqual(result.measure.findtext("./note/duration"), "16")
        self.assertEqual(result.measure.findtext("./note/pitch/step"), "C")
        self.assertEqual(measure.findtext("./note/duration"), "20")

    def test_deterministic_timing_repair_rejects_missing_notation_evidence(self) -> None:
        measure = ET.fromstring(
            """
            <measure number="1">
              <note><rest/><duration>20</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            """
        )

        result = repair.preview_deterministic_timing_repair(measure, 4, 4, 4)

        self.assertFalse(result.applied)
        self.assertIsNone(result.measure)

    def test_timing_review_decision_applies_repair_to_target_part_and_measure(self) -> None:
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1">
                  <attributes><divisions>4</divisions></attributes>
                  <note><rest/><duration>20</duration><type>whole</type></note>
                </measure>
              </part>
              <part id="P2">
                <measure number="1">
                  <attributes><divisions>4</divisions></attributes>
                  <note><rest/><duration>20</duration><type>whole</type></note>
                </measure>
              </part>
            </score-partwise>
            """
        )
        decisions = [
            {
                "action": "correct",
                "kind": "timing_measure_overflow",
                "part_id": "P1",
                "measure_start": 1,
            }
        ]

        applied = repair.apply_deterministic_timing_decisions(
            root,
            repair.ScoreStructurePlan.from_dict({}),
            decisions,
        )

        self.assertEqual(applied, 1)
        self.assertEqual(root.findtext("./part[@id='P1']/measure/note/duration"), "16")
        self.assertEqual(root.findtext("./part[@id='P2']/measure/note/duration"), "20")

    def test_clef_review_decision_changes_only_matching_mid_measure_event(self) -> None:
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1">
                  <attributes>
                    <divisions>4</divisions>
                    <clef number="2"><sign>F</sign><line>4</line></clef>
                  </attributes>
                  <note><rest/><duration>8</duration><voice>1</voice><staff>2</staff></note>
                  <attributes>
                    <clef number="2"><sign>G</sign><line>2</line></clef>
                  </attributes>
                  <note><rest/><duration>8</duration><voice>1</voice><staff>2</staff></note>
                </measure>
              </part>
            </score-partwise>
            """
        )

        applied = repair.apply_reviewed_clef_decisions(
            root,
            [
                {
                    "action": "correct",
                    "kind": "clef_change_mid_measure",
                    "part_id": "P1",
                    "measure_start": 1,
                    "staff": 2,
                    "offset_units": 8,
                    "suggestion": {"sign": "F", "line": 4},
                }
            ],
        )

        clefs = root.findall("./part/measure/attributes/clef[@number='2']")
        self.assertEqual(applied, 1)
        self.assertEqual([clef.findtext("sign") for clef in clefs], ["F", "F"])
        self.assertEqual([clef.findtext("line") for clef in clefs], ["4", "4"])

    def test_structure_plan_resolves_score_measure_ranges(self) -> None:
        plan = repair.ScoreStructurePlan.from_dict(
            {
                "default_time_signature": "4/4",
                "time_signature_changes": [
                    {"from_measure": 25, "to_measure": 25, "signature": "2/4"},
                    {"from_measure": 26, "signature": "4/4"},
                ],
                "clef_overrides": [
                    {"staff": 2, "from_measure": 14, "to_measure": 16, "sign": "G", "line": 2},
                    {"staff": 2, "from_measure": 17, "sign": "F", "line": 4},
                ],
                "key_signature": {"fifths": -5},
            }
        )

        self.assertEqual(plan.time_signature_for(24), (4, 4))
        self.assertEqual(plan.time_signature_for(25), (2, 4))
        self.assertEqual(plan.time_signature_for(26), (4, 4))
        self.assertEqual(plan.clef_for(2, 15), ("G", 2))
        self.assertEqual(plan.clef_for(2, 17), ("F", 4))
        self.assertEqual(plan.key_signature_fifths, -5)

    def test_structure_plan_applies_time_and_clef_changes_by_full_score_measure(self) -> None:
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
              <part id="P1">
                <measure number="1">
                  <attributes><divisions>4</divisions><staves>2</staves>
                    <key><fifths>0</fifths></key>
                    <clef number="1"><sign>G</sign><line>2</line></clef>
                    <clef number="2"><sign>F</sign><line>4</line></clef>
                    <time><beats>4</beats><beat-type>4</beat-type></time>
                  </attributes>
                  <note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note>
                </measure>
              </part>
            </score-partwise>
            """
        )
        part = root.find("part")
        assert part is not None
        first_measure = part.find("measure")
        assert first_measure is not None
        for measure_number in range(2, 27):
            measure = ET.Element("measure", number=str(measure_number))
            attributes = ET.SubElement(measure, "attributes")
            ET.SubElement(attributes, "divisions").text = "4"
            note = ET.SubElement(measure, "note")
            ET.SubElement(note, "rest")
            ET.SubElement(note, "duration").text = "8" if measure_number == 25 else "16"
            ET.SubElement(note, "voice").text = "1"
            ET.SubElement(note, "staff").text = "1"
            part.append(measure)

        plan = repair.ScoreStructurePlan.from_dict(
            {
                "default_time_signature": "4/4",
                "time_signature_changes": [
                    {"from_measure": 25, "to_measure": 25, "signature": "2/4"},
                    {"from_measure": 26, "signature": "4/4"},
                ],
                "clef_overrides": [
                    {"staff": 2, "from_measure": 14, "to_measure": 16, "sign": "G", "line": 2},
                    {"staff": 2, "from_measure": 17, "sign": "F", "line": 4},
                ],
                "key_signature": {"fifths": -5},
            }
        )

        report = repair.fix_musicxml_tree(root, "4/4", structure_plan=plan)

        measures = part.findall("measure")
        self.assertEqual(measures[24].findtext("./attributes/time/beats"), "2")
        self.assertEqual(measures[24].findtext("./attributes/time/beat-type"), "4")
        self.assertEqual(measures[25].findtext("./attributes/time/beats"), "4")
        self.assertEqual(measures[13].findtext("./attributes/clef[@number='2']/sign"), "G")
        self.assertEqual(measures[15].findtext("./attributes/clef[@number='2']/sign"), "G")
        self.assertEqual(measures[16].findtext("./attributes/clef[@number='2']/sign"), "F")
        self.assertEqual(measures[0].findtext("./attributes/key/fifths"), "-5")
        self.assertEqual(report.unsupported_measures, 0)
        self.assertEqual(report.time_signature_changes, [
            {"measure": 1, "signature": "4/4"},
            {"measure": 25, "signature": "2/4"},
            {"measure": 26, "signature": "4/4"},
        ])

    def test_rebuilds_forward_and_independent_staff_lanes(self) -> None:
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1">
                  <attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
                  <note><rest/><duration>8</duration><voice>1</voice><staff>1</staff></note>
                  <backup><duration>8</duration></backup>
                  <forward><duration>4</duration></forward>
                  <note><pitch><step>C</step><octave>3</octave></pitch><duration>8</duration><voice>5</voice><staff>2</staff></note>
                </measure>
              </part>
            </score-partwise>
            """
        )

        report = repair.fix_musicxml_tree(root, "4/4")
        measure = root.find("./part/measure")
        assert measure is not None

        self.assertEqual(report.measures_rebuilt, 1)
        self.assertEqual(report.unsupported_measures, 0)
        self.assertEqual(repair.measure_cursor_extent(measure), 16)
        self.assertEqual([note.findtext("duration") for note in measure.findall("note")], ["8", "8"])
        self.assertEqual([backup.findtext("duration") for backup in measure.findall("backup")], ["16"])
        self.assertIn("8", [forward.findtext("duration") for forward in measure.findall("forward")])

    def test_does_not_rebuild_lane_that_exceeds_measure_boundary(self) -> None:
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1">
                  <attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
                  <note><rest/><duration>20</duration><voice>1</voice><staff>1</staff></note>
                  <backup><duration>20</duration></backup>
                  <note><rest/><duration>16</duration><voice>5</voice><staff>2</staff></note>
                </measure>
              </part>
            </score-partwise>
            """
        )

        report = repair.fix_musicxml_tree(root, "4/4")
        measure = root.find("./part/measure")
        assert measure is not None

        self.assertEqual(report.measures_rebuilt, 0)
        self.assertEqual(report.unsupported_measures, 1)
        self.assertEqual([note.findtext("duration") for note in measure.findall("note")], ["20", "16"])
        self.assertTrue(any("exceeds" in warning for warning in report.warnings))

    def test_normalizes_transient_key_and_clef_changes(self) -> None:
        xml = """
        <score-partwise version="4.0">
          <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
          <part id="P1">
            <measure number="1">
              <attributes><divisions>4</divisions><key><fifths>5</fifths></key>
                <clef number="1"><sign>G</sign><line>2</line></clef>
                <clef number="2"><sign>F</sign><line>4</line></clef></attributes>
              <note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            <measure number="2">
              <attributes><key><fifths>0</fifths></key>
                <clef number="1"><sign>F</sign><line>4</line></clef>
                <clef number="2"><sign>G</sign><line>2</line></clef></attributes>
              <note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            <measure number="3">
              <attributes><key><fifths>5</fifths></key>
                <clef number="1"><sign>G</sign><line>2</line></clef>
                <clef number="2"><sign>F</sign><line>4</line></clef></attributes>
              <note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            <measure number="4">
              <attributes><key><fifths>2</fifths></key></attributes>
              <note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            <measure number="5">
              <attributes><key><fifths>2</fifths></key></attributes>
              <note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note>
            </measure>
          </part>
        </score-partwise>
        """
        root = ET.fromstring(xml)
        report = repair.fix_musicxml_tree(root, "4/4")

        measures = root.findall("./part/measure")
        self.assertEqual(measures[1].findtext("./attributes/key/fifths"), "5")
        self.assertEqual(measures[1].findtext("./attributes/clef[1]/sign"), "G")
        self.assertEqual(measures[1].findtext("./attributes/clef[2]/sign"), "F")
        self.assertEqual(measures[3].findtext("./attributes/key/fifths"), "2")
        self.assertEqual(report.transient_key_signatures_normalized, 1)
        self.assertEqual(report.transient_clefs_normalized, 2)

    def test_fix_rebuilds_measure_cursor_without_changing_notes(self) -> None:
        tree = _build_sample_score()

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "broken.musicxml"
            target = Path(temp_dir) / "fixed.musicxml"
            tree.write(source, encoding="unicode", xml_declaration=True)

            report = repair.fix_musicxml_file(source, target, "4/4")

            fixed_tree = ET.parse(target)
            measure = fixed_tree.getroot().find("./part/measure")
            assert measure is not None

            time_nodes = measure.findall("./attributes/time")
            self.assertEqual(len(time_nodes), 1)
            self.assertEqual(time_nodes[0].findtext("beats"), "4")
            self.assertEqual(time_nodes[0].findtext("beat-type"), "4")

            self.assertEqual(report.measures_rebuilt, 1)
            self.assertEqual(report.duplicate_time_signatures_removed, 1)
            self.assertEqual(repair.measure_cursor_extent(measure), 16)

            note_signatures = [
                (
                    note.findtext("staff", "1"),
                    note.findtext("voice", "1"),
                    note.findtext("duration", "0"),
                    note.findtext("./pitch/step", "rest"),
                )
                for note in measure.findall("note")
            ]
            self.assertEqual(
                note_signatures,
                [
                    ("1", "1", "2", "rest"),
                    ("1", "1", "14", "E"),
                    ("2", "5", "1", "A"),
                    ("2", "5", "15", "E"),
                ],
            )
            backup_durations = [backup.findtext("duration") for backup in measure.findall("backup")]
            self.assertEqual(backup_durations, ["16"])

    def test_fix_normalizes_constant_tempo_metadata(self) -> None:
        tree = _build_sample_score()
        root = tree.getroot()
        measure = root.find("./part/measure")
        assert measure is not None

        direction = ET.Element("direction")
        direction_type = ET.SubElement(direction, "direction-type")
        metronome = ET.SubElement(direction_type, "metronome")
        ET.SubElement(metronome, "beat-unit").text = "quarter"
        ET.SubElement(metronome, "per-minute").text = "120"
        ET.SubElement(direction, "sound", tempo="120")
        measure.insert(1, direction)

        extra_direction = ET.Element("direction")
        extra_direction_type = ET.SubElement(extra_direction, "direction-type")
        extra_metronome = ET.SubElement(extra_direction_type, "metronome")
        ET.SubElement(extra_metronome, "beat-unit").text = "quarter"
        ET.SubElement(extra_metronome, "per-minute").text = "90"
        ET.SubElement(extra_direction, "sound", tempo="90")
        measure.append(extra_direction)

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "broken.musicxml"
            target = Path(temp_dir) / "fixed.musicxml"
            tree.write(source, encoding="unicode", xml_declaration=True)

            report = repair.fix_musicxml_file(source, target, "4/4", 125)

            fixed_tree = ET.parse(target)
            fixed_measure = fixed_tree.getroot().find("./part/measure")
            assert fixed_measure is not None

            metronomes = fixed_measure.findall("./direction/direction-type/metronome")
            self.assertEqual(len(metronomes), 1)
            self.assertEqual(metronomes[0].findtext("per-minute"), "125")

            sounds = fixed_measure.findall("./direction/sound")
            self.assertEqual(len(sounds), 1)
            self.assertEqual(sounds[0].get("tempo"), "125")

            self.assertEqual(report.tempo_markings_removed, 2)
            self.assertEqual(report.tempo_markings_inserted, 1)


class RepairMidiTest(unittest.TestCase):
    def test_fix_midi_normalizes_metadata_preserving_notes(self) -> None:
        midi_obj = miditoolkit.MidiFile()
        midi_obj.ticks_per_beat = 480
        midi_obj.time_signature_changes = [
            miditoolkit.TimeSignature(4, 4, 0),
            miditoolkit.TimeSignature(3, 4, 480),
            miditoolkit.TimeSignature(6, 8, 960),
        ]
        midi_obj.tempo_changes = [
            miditoolkit.TempoChange(120.0, 0),
            miditoolkit.TempoChange(90.0, 480),
        ]
        track = miditoolkit.Instrument(program=0, is_drum=False, name="Piano")
        track.notes.append(miditoolkit.Note(velocity=100, pitch=60, start=0, end=480))
        track.notes.append(miditoolkit.Note(velocity=100, pitch=64, start=480, end=960))
        midi_obj.instruments.append(track)

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "raw.mid"
            target = Path(temp_dir) / "fixed.mid"
            midi_obj.dump(str(source))

            report = repair.fix_midi_file(source, target, "3/4", 125)

            fixed = miditoolkit.MidiFile(str(target))
            self.assertEqual(len(fixed.time_signature_changes), 1)
            self.assertEqual((fixed.time_signature_changes[0].numerator, fixed.time_signature_changes[0].denominator), (3, 4))
            self.assertEqual(fixed.time_signature_changes[0].time, 0)

            self.assertEqual(len(fixed.tempo_changes), 1)
            self.assertEqual(fixed.tempo_changes[0].tempo, 125.0)
            self.assertEqual(fixed.tempo_changes[0].time, 0)

            notes = fixed.instruments[0].notes
            self.assertEqual([n.pitch for n in notes], [60, 64])
            self.assertEqual([(n.start, n.end) for n in notes], [(0, 480), (480, 960)])

            self.assertEqual(report.original_time_signature_count, 3)
            self.assertEqual(report.removed_time_signature_count, 2)
            self.assertEqual(report.original_tempo_change_count, 2)
            self.assertEqual(report.removed_tempo_change_count, 1)

    def test_fix_midi_writes_structure_time_signatures_at_measure_starts(self) -> None:
        midi_obj = miditoolkit.MidiFile()
        midi_obj.ticks_per_beat = 480
        midi_obj.time_signature_changes = [
            miditoolkit.TimeSignature(9, 8, 0),
            miditoolkit.TimeSignature(7, 16, 480),
        ]
        track = miditoolkit.Instrument(program=0, is_drum=False, name="Piano")
        track.notes.append(miditoolkit.Note(velocity=100, pitch=60, start=0, end=480))
        midi_obj.instruments.append(track)

        plan = repair.ScoreStructurePlan.from_dict(
            {
                "default_time_signature": "4/4",
                "time_signature_changes": [
                    {"from_measure": 25, "to_measure": 25, "signature": "2/4"},
                    {"from_measure": 26, "signature": "4/4"},
                ],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "raw.mid"
            target = Path(temp_dir) / "fixed.mid"
            midi_obj.dump(str(source))

            report = repair.fix_midi_file(
                source,
                target,
                "4/4",
                structure_plan=plan,
                measure_count=30,
            )

            fixed = miditoolkit.MidiFile(str(target))
            self.assertEqual(
                [(item.time, item.numerator, item.denominator) for item in fixed.time_signature_changes],
                [(0, 4, 4), (46080, 2, 4), (47040, 4, 4)],
            )
            self.assertEqual(report.inserted_time_signature_count, 3)


if __name__ == "__main__":
    unittest.main()
