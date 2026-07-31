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


if __name__ == "__main__":
    unittest.main()
