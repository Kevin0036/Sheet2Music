import unittest
import xml.etree.ElementTree as ET

from homr.music_xml_generator import XmlGeneratorArguments, generate_xml
from homr.transformer.vocabulary import EncodedSymbol


def _symbol(
    rhythm: str,
    *,
    pitch: str = ".",
    lift: str = ".",
    articulation: str = ".",
    slur: str = ".",
    position: str = ".",
) -> EncodedSymbol:
    return EncodedSymbol(
        rhythm,
        pitch=pitch,
        lift=lift,
        articulation=articulation,
        slur=slur,
        position=position,
    )


def _note(rhythm: str, pitch: str, position: str, *, lift: str = "_", slur: str = "_") -> EncodedSymbol:
    return _symbol(
        rhythm,
        pitch=pitch,
        lift=lift,
        articulation="_",
        slur=slur,
        position=position,
    )


def _rest(rhythm: str, position: str) -> EncodedSymbol:
    return _symbol(
        rhythm,
        pitch="_",
        lift="_",
        articulation="_",
        slur="_",
        position=position,
    )


def _measure_extent(measure: ET.Element) -> int:
    current_time = 0
    max_time = 0
    last_note_start = 0
    for child in measure:
        if child.tag == "backup":
            duration = int(child.findtext("duration", "0"))
            current_time -= duration
            continue
        if child.tag == "forward":
            duration = int(child.findtext("duration", "0"))
            current_time += duration
            max_time = max(max_time, current_time)
            continue
        if child.tag != "note":
            continue

        duration = int(child.findtext("duration", "0"))
        is_chord = child.find("chord") is not None
        start = last_note_start if is_chord else current_time
        if not is_chord:
            last_note_start = start
            current_time += duration
        max_time = max(max_time, start + duration)
    return max_time


def _expected_measure_duration(measure: ET.Element) -> int:
    attributes = measure.find("attributes")
    assert attributes is not None
    divisions = int(attributes.findtext("divisions", "1"))
    time = next(
        child.find("time")
        for child in measure
        if child.tag == "attributes" and child.find("time") is not None
    )
    beats = int(time.findtext("beats", "0"))
    beat_type = int(time.findtext("beat-type", "4"))
    return beats * divisions * 4 // beat_type


def _first_measure(xml: ET.Element) -> ET.Element:
    part = xml.find("part")
    assert part is not None
    measure = part.find("measure")
    assert measure is not None
    return measure


class TestMusicXmlGrandStaffTiming(unittest.TestCase):
    def test_grand_staff_measure_does_not_overflow(self) -> None:
        symbols = [
            _symbol("clef_G2", position="upper"),
            EncodedSymbol("chord"),
            _symbol("clef_F4", position="lower"),
            _symbol("keySignature_4"),
            _symbol("timeSignature/4"),
            _rest("rest_8", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "A2", "lower"),
            EncodedSymbol("chord"),
            _note("note_8", "E3", "lower"),
            _note("note_8", "E4", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "B3", "lower", slur="slurStart"),
            _note("note_8", "E4", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "B3", "lower", slur="slurStop"),
            EncodedSymbol("chord"),
            _note("note_16", "E3", "lower"),
            _note("note_8", "E4", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "G3", "lower", lift="#"),
            EncodedSymbol("chord"),
            _note("note_16", "B2", "lower", slur="slurStart"),
            _note("note_16", "E4", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "B2", "lower", slur="slurStop"),
            _note("note_8", "E4", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "E3", "lower"),
            EncodedSymbol("chord"),
            _note("note_16", "F3", "lower", lift="#"),
            _note("note_16", "E4", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "B3", "lower", slur="slurStart"),
            _note("note_8", "E4", "upper"),
            EncodedSymbol("chord"),
            _note("note_16", "B3", "lower", slur="slurStop"),
            EncodedSymbol("chord"),
            _note("note_16", "B2", "lower"),
            _note("note_8", "B3", "upper", slur="slurStart"),
            EncodedSymbol("chord"),
            _note("note_8", "F3", "lower", lift="#"),
            _symbol("barline"),
        ]

        xml = generate_xml(XmlGeneratorArguments(), [symbols], "")
        measure = _first_measure(xml)

        self.assertEqual(_measure_extent(measure), _expected_measure_duration(measure))

        time_elements = [
            child.find("time")
            for child in measure
            if child.tag == "attributes" and child.find("time") is not None
        ]
        self.assertEqual(len(time_elements), 1)


if __name__ == "__main__":
    unittest.main()
