import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import sheet2music.core.auto_resolution as auto_resolution

from sheet2music.core.auto_resolution import (
    AutoResolutionBatch,
    AutoResolutionRunner,
    AutoResolutionStore,
    BatchStatus,
    BatchRunContext,
    CandidateArtifact,
    CandidateEvidence,
    CandidateValidation,
    TransactionContext,
    apply_candidate_transactionally,
    build_image_variants,
    candidate_fingerprint,
    choose_candidate,
    validate_batch_candidate_artifact,
    validate_candidate,
)
from sheet2music.core.structure import ScoreStructurePlan


def _score(*, second_duration: int = 16, second_clef: str = "F") -> ET.Element:
    return ET.fromstring(
        f"""
        <score-partwise version="4.0">
          <part id="P1">
            <measure number="1">
              <attributes>
                <divisions>4</divisions>
                <time><beats>4</beats><beat-type>4</beat-type></time>
                <clef number="2"><sign>F</sign><line>4</line></clef>
              </attributes>
              <note><rest/><duration>16</duration><voice>1</voice><staff>2</staff></note>
            </measure>
            <measure number="2">
              <attributes><clef number="2"><sign>{second_clef}</sign><line>{'2' if second_clef == 'G' else '4'}</line></clef></attributes>
              <note><rest/><duration>{second_duration}</duration><voice>1</voice><staff>2</staff></note>
            </measure>
          </part>
        </score-partwise>
        """
    )


def _batch() -> AutoResolutionBatch:
    return AutoResolutionBatch(
        batch_id="page-1-system-0",
        page_number=1,
        system_index=0,
        target_measures=(2,),
        context_range=(1, 2),
    )


def _evidence(*, strong: bool = True) -> CandidateEvidence:
    return CandidateEvidence(
        variant="standard",
        mapping_confidence="high" if strong else "ambiguous",
        source_notehead_counts=(1, 1),
        candidate_notehead_counts=(1, 1),
        context_anchors_aligned=True,
    )


def _validation(
    candidate_id: str,
    fingerprint: str,
    *,
    accepted: bool = True,
    strong: bool = False,
) -> CandidateValidation:
    return CandidateValidation(
        candidate_id=candidate_id,
        accepted=accepted,
        reasons=(),
        fingerprint=fingerprint,
        target_findings_before=1,
        target_findings_after=0,
        has_strong_single_candidate_evidence=strong,
    )


class CandidateValidationTest(unittest.TestCase):
    def _validate_stored_candidate(
        self,
        base: ET.Element,
        candidate: ET.Element,
        batch: AutoResolutionBatch,
    ) -> CandidateValidation:
        from sheet2music.core.workspace import JobWorkspace

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            base_path = workspace.output_dir / "score.auto.musicxml"
            candidate_path = workspace.auto_resolution_candidate_dir / "candidate.musicxml"
            page_layout = workspace.layout_dir / "page-1.json"
            candidate_layout = workspace.auto_resolution_candidate_dir / "candidate.layout.json"
            ET.ElementTree(base).write(base_path, encoding="utf-8")
            ET.ElementTree(candidate).write(candidate_path, encoding="utf-8")
            (workspace.page_geometry_dir / "page-1.json").write_text(
                '{"schema_version":1,"raw_size":{"width":100,"height":100},"input_bounds_in_raw":[0,0,100,100],"input_size":{"width":100,"height":100}}',
                encoding="utf-8",
            )
            page_layout.write_text(
                '{"schema_version":1,"transform":{"source_size":[100,100],"autocrop_bounds":[0,0,100,100],"recognition_size":[100,100]},"systems":[{"system_index":0,"bbox":[0,0,100,100],"staff_bboxes":[[0,10,100,30],[0,50,100,70]],"local_measure_start":1,"local_measure_end":2,"measure_notehead_counts":[1,1],"mapping_confidence":"high"}]}',
                encoding="utf-8",
            )
            candidate_layout.write_text(
                '{"systems":[{"mapping_confidence":"high","measure_notehead_counts":[1,1]}]}',
                encoding="utf-8",
            )
            return validate_batch_candidate_artifact(
                workspace=workspace,
                base_xml=base_path,
                candidate_xml=candidate_path,
                candidate_layout=candidate_layout,
                batch=batch,
                page_layouts=(page_layout,),
                page_measure_offsets=(0,),
                structure_plan=ScoreStructurePlan.from_dict({}),
                candidate_id="standard",
            )

    def test_normalization_ignores_layout_metadata_but_keeps_music(self) -> None:
        xml_a = ET.fromstring(
            """
            <score-partwise><part id="P1"><measure number="1" width="120.0">
              <print new-system="yes"/>
              <note default-x="42.0"><pitch><step>C</step><octave>4.0</octave></pitch><duration>4.0</duration></note>
            </measure></part></score-partwise>
            """
        )
        xml_b = ET.fromstring(
            """
            <score-partwise><part id="P1"><measure number="1">
              <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration></note>
            </measure></part></score-partwise>
            """
        )
        changed_music = ET.fromstring(
            """
            <score-partwise><part id="P1"><measure number="1">
              <note><pitch><step>D</step><octave>4</octave></pitch><duration>4</duration></note>
            </measure></part></score-partwise>
            """
        )

        self.assertEqual(candidate_fingerprint(xml_a), candidate_fingerprint(xml_b))
        self.assertNotEqual(candidate_fingerprint(xml_a), candidate_fingerprint(changed_music))

    def test_candidate_rejects_overflow_and_unconfirmed_clef_change(self) -> None:
        result = validate_candidate(
            _score(second_duration=20, second_clef="G"),
            _batch(),
            _evidence(),
            ScoreStructurePlan.from_dict({}),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(set(result.reasons), {"measure_overflow", "structure_changed"})
        self.assertEqual(result.target_findings_before, 1)
        self.assertEqual(result.target_findings_after, 1)

    def test_underfilled_voice_is_not_rejected(self) -> None:
        result = validate_candidate(
            _score(second_duration=8),
            _batch(),
            _evidence(),
            ScoreStructurePlan.from_dict({}),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reasons, ())
        self.assertTrue(result.has_strong_single_candidate_evidence)

    def test_visual_notehead_mismatch_is_a_hard_gate(self) -> None:
        evidence = CandidateEvidence(
            variant="standard",
            mapping_confidence="high",
            source_notehead_counts=(1, 6),
            candidate_notehead_counts=(1, 1),
            context_anchors_aligned=True,
            notehead_tolerance=1,
        )

        result = validate_candidate(
            _score(second_duration=8),
            _batch(),
            evidence,
            ScoreStructurePlan.from_dict({}),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("visual_notehead_mismatch",))

    def test_validates_uploaded_artifact_against_persisted_page_layout(self) -> None:
        from sheet2music.core.workspace import JobWorkspace

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            base_path = workspace.output_dir / "score.auto.musicxml"
            candidate_path = workspace.auto_resolution_candidate_dir / "upload.musicxml"
            page_layout = workspace.layout_dir / "page-1.json"
            candidate_layout = workspace.auto_resolution_candidate_dir / "upload.layout.json"
            ET.ElementTree(_score(second_duration=8)).write(base_path, encoding="utf-8")
            ET.ElementTree(_score(second_duration=8)).write(candidate_path, encoding="utf-8")
            (workspace.page_geometry_dir / "page-1.json").write_text(
                '{"schema_version":1,"raw_size":{"width":100,"height":100},"input_bounds_in_raw":[0,0,100,100],"input_size":{"width":100,"height":100}}',
                encoding="utf-8",
            )
            page_layout.write_text(
                '{"schema_version":1,"transform":{"source_size":[100,100],"autocrop_bounds":[0,0,100,100],"recognition_size":[100,100]},"systems":[{"system_index":0,"bbox":[0,0,100,100],"staff_bboxes":[[0,10,100,30],[0,50,100,70]],"local_measure_start":1,"local_measure_end":2,"measure_notehead_counts":[1,1],"mapping_confidence":"high"}]}',
                encoding="utf-8",
            )
            candidate_layout.write_text(
                '{"systems":[{"mapping_confidence":"high","measure_notehead_counts":[1,1]}]}',
                encoding="utf-8",
            )

            result = validate_batch_candidate_artifact(
                workspace=workspace,
                base_xml=base_path,
                candidate_xml=candidate_path,
                candidate_layout=candidate_layout,
                batch=_batch(),
                page_layouts=(page_layout,),
                page_measure_offsets=(0,),
                structure_plan=ScoreStructurePlan.from_dict({}),
                candidate_id="user-upload",
            )

        self.assertTrue(result.accepted)

    def test_rejects_clef_change_in_first_target_measure(self) -> None:
        base = _score(second_duration=8)
        candidate = _score(second_duration=8)
        for root in (base, candidate):
            second_attributes = root.find("./part/measure[2]/attributes")
            assert second_attributes is not None
            second_clef = second_attributes.find("clef")
            assert second_clef is not None
            second_attributes.remove(second_clef)
        first_clef = candidate.find("./part/measure/attributes/clef")
        assert first_clef is not None
        first_clef.find("sign").text = "G"
        first_clef.find("line").text = "2"
        batch = AutoResolutionBatch(
            batch_id="page-1-system-0",
            page_number=1,
            system_index=0,
            target_measures=(1,),
            context_range=(1, 2),
        )

        result = self._validate_stored_candidate(base, candidate, batch)

        self.assertFalse(result.accepted)
        self.assertIn("structure_changed", result.reasons)

    def test_rejects_key_change_in_first_target_measure(self) -> None:
        base = _score(second_duration=8)
        candidate = _score(second_duration=8)
        attributes = candidate.find("./part/measure/attributes")
        assert attributes is not None
        key = ET.SubElement(attributes, "key")
        ET.SubElement(key, "fifths").text = "3"
        batch = AutoResolutionBatch(
            batch_id="page-1-system-0",
            page_number=1,
            system_index=0,
            target_measures=(1,),
            context_range=(1, 2),
        )

        result = self._validate_stored_candidate(base, candidate, batch)

        self.assertFalse(result.accepted)
        self.assertIn("structure_changed", result.reasons)

    def test_rejects_key_removal_in_first_target_measure(self) -> None:
        base = _score(second_duration=8)
        base_attributes = base.find("./part/measure/attributes")
        assert base_attributes is not None
        key = ET.SubElement(base_attributes, "key")
        ET.SubElement(key, "fifths").text = "-2"
        candidate = _score(second_duration=8)
        batch = AutoResolutionBatch(
            batch_id="page-1-system-0",
            page_number=1,
            system_index=0,
            target_measures=(1,),
            context_range=(1, 2),
        )

        result = self._validate_stored_candidate(base, candidate, batch)

        self.assertFalse(result.accepted)
        self.assertIn("structure_changed", result.reasons)

    def test_rejects_unconfirmed_time_change_to_structure_plan_default(self) -> None:
        base = _score(second_duration=8)
        base_time = base.find("./part/measure/attributes/time")
        assert base_time is not None
        base_time.find("beats").text = "2"
        candidate = _score(second_duration=8)
        batch = AutoResolutionBatch(
            batch_id="page-1-system-0",
            page_number=1,
            system_index=0,
            target_measures=(1,),
            context_range=(1, 2),
        )

        result = self._validate_stored_candidate(base, candidate, batch)

        self.assertFalse(result.accepted)
        self.assertIn("structure_changed", result.reasons)


class CandidateSelectionTest(unittest.TestCase):
    def test_two_variants_with_same_candidate_auto_resolve(self) -> None:
        choice = choose_candidate(
            [_validation("standard", "same"), _validation("contrast", "same")]
        )

        self.assertEqual(choice.status, BatchStatus.AUTO_RESOLVED)
        self.assertEqual(choice.selected_candidate, "standard")
        self.assertEqual(choice.reason, "two_variants_agree")

    def test_conflicting_valid_candidates_require_user_choice(self) -> None:
        choice = choose_candidate(
            [_validation("standard", "a"), _validation("contrast", "b")]
        )

        self.assertEqual(choice.status, BatchStatus.NEEDS_CHOICE)
        self.assertEqual(choice.candidate_ids, ("standard", "contrast"))

    def test_single_candidate_needs_strong_visual_evidence(self) -> None:
        strong = choose_candidate([_validation("standard", "a", strong=True)])
        weak = choose_candidate([_validation("standard", "a", strong=False)])

        self.assertEqual(strong.status, BatchStatus.AUTO_RESOLVED)
        self.assertEqual(strong.reason, "only_valid_candidate_with_visual_evidence")
        self.assertEqual(weak.status, BatchStatus.NEEDS_UPLOAD)


class ImageVariantTest(unittest.TestCase):
    def test_builds_three_distinct_deterministic_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            image = np.full((80, 160, 3), 255, dtype=np.uint8)
            cv2.line(image, (10, 20), (150, 20), (90, 90, 90), 2)
            cv2.circle(image, (60, 40), 8, (150, 150, 150), -1)
            cv2.imwrite(str(source), image)

            first = build_image_variants(source, root / "first")
            second = build_image_variants(source, root / "second")

        self.assertEqual([item.name for item in first], ["standard", "contrast", "context"])
        self.assertEqual(len({item.digest for item in first}), 3)
        self.assertEqual(
            [(item.name, item.digest) for item in first],
            [(item.name, item.digest) for item in second],
        )


class BatchPersistenceTest(unittest.TestCase):
    def test_batch_store_round_trips_completed_attempts_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "batches.json"
            store = AutoResolutionStore(state_path)
            batch = _batch()
            batch.attempts.append({"variant": "standard", "status": "succeeded"})

            store.save([batch])
            restored = store.load()

            self.assertEqual(restored[0].attempts[0]["variant"], "standard")
            self.assertFalse(state_path.with_suffix(".json.tmp").exists())

    def test_runner_does_not_rerun_completed_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            cv2.imwrite(str(source), np.full((40, 80, 3), 255, dtype=np.uint8))
            batch = _batch()
            batch.attempts.append({"variant": "standard", "status": "succeeded"})
            called: list[str] = []

            def fake_homr(image: Path, **kwargs: object) -> Path:
                called.append(image.stem)
                layout_output = kwargs["layout_output"]
                assert isinstance(layout_output, Path)
                layout_output.parent.mkdir(parents=True, exist_ok=True)
                layout_output.write_text("{}", encoding="utf-8")
                output = Path(kwargs["work_dir"]) / f"{image.stem}.musicxml"
                output.parent.mkdir(parents=True, exist_ok=True)
                ET.ElementTree(_score()).write(output, encoding="utf-8")
                return output

            store = AutoResolutionStore(root / "batches.json")
            runner = AutoResolutionRunner(store, homr_runner=fake_homr)
            runner.resolve_batch(
                batch,
                BatchRunContext(
                    source_crop=source,
                    crop_dir=root / "crops",
                    candidate_dir=root / "candidates",
                    work_dir=root / "work",
                    use_gpu=False,
                    gpu_available=False,
                ),
            )

        self.assertNotIn("standard", called)
        self.assertEqual(called, ["contrast", "context"])

    def test_reconcile_preserves_only_exact_matching_batch_state(self) -> None:
        exact = _batch()
        exact.attempts.append({"variant": "standard", "status": "succeeded"})
        stale = AutoResolutionBatch(
            batch_id="old",
            page_number=1,
            system_index=1,
            target_measures=(3,),
            context_range=(3, 4),
        )
        specs = [
            mock.Mock(
                batch_id=exact.batch_id,
                page_number=exact.page_number,
                system_index=exact.system_index,
                target_measures=exact.target_measures,
                context_range=exact.context_range,
            ),
            mock.Mock(
                batch_id="new",
                page_number=2,
                system_index=0,
                target_measures=(5,),
                context_range=(5, 6),
            ),
        ]

        reconciled = auto_resolution.reconcile_batches(specs, [exact, stale])

        self.assertEqual([item.batch_id for item in reconciled], [exact.batch_id, "new"])
        self.assertEqual(reconciled[0].attempts, exact.attempts)
        self.assertEqual(reconciled[1].status, BatchStatus.PENDING)

    def test_reconcile_resets_state_when_context_changes(self) -> None:
        persisted = _batch()
        persisted.attempts.append({"variant": "standard", "status": "succeeded"})
        changed = mock.Mock(
            batch_id=persisted.batch_id,
            page_number=persisted.page_number,
            system_index=persisted.system_index,
            target_measures=persisted.target_measures,
            context_range=(2, 2),
        )

        reconciled = auto_resolution.reconcile_batches([changed], [persisted])

        self.assertEqual(reconciled[0].context_range, (2, 2))
        self.assertEqual(reconciled[0].attempts, [])
        self.assertEqual(reconciled[0].status, BatchStatus.PENDING)

    def test_reconcile_keeps_resolved_and_committing_history(self) -> None:
        resolved = _batch()
        resolved.status = BatchStatus.AUTO_RESOLVED
        committing = AutoResolutionBatch(
            batch_id="committing",
            page_number=2,
            system_index=0,
            target_measures=(5,),
            context_range=(5, 6),
            status=BatchStatus.COMMITTING,
        )

        reconciled = auto_resolution.reconcile_batches([], [resolved, committing])

        self.assertEqual(
            [item.batch_id for item in reconciled],
            [resolved.batch_id, committing.batch_id],
        )


class TransactionTest(unittest.TestCase):
    def test_transaction_rolls_back_when_global_findings_increase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "score.musicxml"
            candidate_path = root / "candidate.musicxml"
            base = _score(second_duration=20, second_clef="F")
            candidate = _score(second_duration=16, second_clef="G")
            ET.ElementTree(base).write(base_path, encoding="utf-8", xml_declaration=True)
            ET.ElementTree(candidate).write(candidate_path, encoding="utf-8", xml_declaration=True)
            original_bytes = base_path.read_bytes()

            result = apply_candidate_transactionally(
                TransactionContext(
                    base_xml_path=base_path,
                    validation_dir=root / "validation",
                    structure_plan=ScoreStructurePlan.from_dict({}),
                ),
                _batch(),
                CandidateArtifact(
                    candidate_id="standard",
                    xml_path=candidate_path,
                    candidate_global_start=1,
                ),
            )

            self.assertFalse(result.committed)
            self.assertEqual(result.reason, "new_high_risk_findings")
            self.assertEqual(base_path.read_bytes(), original_bytes)
            self.assertTrue((root / "validation" / "page-1-system-0.json").exists())

    def test_transaction_rolls_back_when_boundary_validation_fails_with_other_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "score.musicxml"
            candidate_path = root / "candidate.musicxml"
            base = _score(second_duration=20)
            first_duration = base.find("./part/measure/note/duration")
            assert first_duration is not None
            first_duration.text = "20"
            candidate = _score(second_duration=16)
            ET.ElementTree(base).write(base_path, encoding="utf-8", xml_declaration=True)
            ET.ElementTree(candidate).write(candidate_path, encoding="utf-8", xml_declaration=True)
            original_bytes = base_path.read_bytes()

            result = apply_candidate_transactionally(
                TransactionContext(
                    base_xml_path=base_path,
                    validation_dir=root / "validation",
                    structure_plan=ScoreStructurePlan.from_dict({}),
                ),
                _batch(),
                CandidateArtifact(
                    candidate_id="standard",
                    xml_path=candidate_path,
                    candidate_global_start=1,
                ),
            )

            self.assertFalse(result.committed)
            self.assertEqual(result.reason, "boundary_validation_failed")
            self.assertEqual(base_path.read_bytes(), original_bytes)

    def test_recovery_finalizes_batch_when_xml_was_replaced_before_status_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "score.musicxml"
            candidate_path = root / "candidate.musicxml"
            store = AutoResolutionStore(root / "batches.json")
            batch = _batch()
            ET.ElementTree(_score(second_duration=20)).write(
                base_path, encoding="utf-8", xml_declaration=True
            )
            ET.ElementTree(_score(second_duration=16)).write(
                candidate_path, encoding="utf-8", xml_declaration=True
            )
            store.save([batch])

            def persist_commit(journal: Mapping[str, object]) -> None:
                batch.status = BatchStatus.COMMITTING
                batch.commit = dict(journal)
                store.save_batch(batch)

            result = apply_candidate_transactionally(
                TransactionContext(
                    base_xml_path=base_path,
                    validation_dir=root / "validation",
                    structure_plan=ScoreStructurePlan.from_dict({}),
                    before_commit=persist_commit,
                ),
                batch,
                CandidateArtifact(
                    candidate_id="standard",
                    xml_path=candidate_path,
                    candidate_global_start=1,
                ),
            )
            self.assertTrue(result.committed)
            self.assertEqual(store.load()[0].status, BatchStatus.COMMITTING)

            auto_resolution.recover_pending_commits(base_path, store)

            restored = store.load()[0]
            self.assertEqual(restored.status, BatchStatus.AUTO_RESOLVED)
            self.assertEqual(restored.selected_candidate, "standard")


if __name__ == "__main__":
    unittest.main()
