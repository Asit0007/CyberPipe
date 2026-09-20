"""review.py: the document the human reads before tapping approve."""
from __future__ import annotations

import unittest

import review

RESEARCH = {"retrievedSources": [
    {"id": "S1", "ok": True, "title": "Openwall disclosure", "url": "https://example.org/oss-security", "via": "direct"},
    {"id": "S2", "ok": False, "title": "", "url": "https://paywalled.example/x", "error": "HTTP 403"}]}
DRAFT = {
    "title": "How a backdoor reached sshd",
    "estimatedTotalDuration": 40,
    "generation": {"complete": False, "producedScenes": 2, "requestedScenes": 47, "degraded": ["Narrative chunk 2/16 failed"]},
    "qualityChecks": [{"severity": "warn", "id": "unsupported-specifics", "message": "CVE-2099-1 is not in the dossier"}],
    "midrollMarkers": [{"index": 1, "timestamp": "2:30", "reason": "after scene 9"}],
    "scenes": [
        {"sceneNumber": 1, "title": "Cold open", "actPhase": "Hook", "durationEst": 12, "narration": "A maintainer added a backdoor.", "citations": ["S1"]},
        {"sceneNumber": 2, "title": "Context", "actPhase": "Context", "durationEst": 28, "narration": "liblzma sits under sshd on many distros."}],
}


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.md = review.render_review_markdown({"id": 7, "pending_payload": DRAFT, "stage_outputs": {"research": RESEARCH}})

    def test_contains_every_scenes_narration_with_running_timestamps(self):
        self.assertIn("A maintainer added a backdoor.", self.md)
        self.assertIn("liblzma sits under sshd on many distros.", self.md)
        self.assertIn("0:00", self.md)
        self.assertIn("0:12", self.md, "scene 2 starts where scene 1 ends")
        self.assertIn("[S1]", self.md)

    def test_leads_with_what_the_approver_most_needs_to_know(self):
        first_screen = self.md[:900]
        self.assertIn("2/47 scenes", first_screen)
        self.assertIn("Narrative chunk 2/16 failed", first_screen)
        self.assertIn("CVE-2099-1 is not in the dossier", first_screen)

    def test_lists_sources_actually_read_and_marks_the_ones_that_failed(self):
        self.assertIn("https://example.org/oss-security", self.md)
        self.assertRegex(self.md, r"paywalled\.example.*(not retrieved|HTTP 403)")

    def test_lists_midroll_placement(self):
        self.assertIn("2:30", self.md)

    def test_survives_a_bare_draft(self):
        md = review.render_review_markdown({"id": 1, "pending_payload": {"scenes": [{"narration": "Only line."}]}, "stage_outputs": {}})
        self.assertIn("Only line.", md)

    def test_returns_none_when_there_is_no_script_to_show(self):
        self.assertIsNone(review.render_review_markdown({"id": 1, "pending_payload": {"topicTitle": "x"}, "stage_outputs": {}}))
        self.assertIsNone(review.render_review_markdown({"id": 1, "pending_payload": {}, "stage_outputs": {}}))


if __name__ == "__main__":
    unittest.main()
