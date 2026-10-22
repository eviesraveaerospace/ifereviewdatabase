"""Tests for the OCR trigger/versioning logic (no OCR models needed: the
ios-screen-recording-ocr checkout is replaced by stubs).

Run: python test_ocr_triggers.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_STUB = tempfile.mkdtemp(prefix="ocr_stub_")
Path(_STUB, "ocr_video_chapters.py").write_text("def analyze_frames(*a, **k):\n    pass\n")
Path(_STUB, "llm_video_chapters.py").write_text("def sample_video(*a, **k):\n    return []\n")
Path(_STUB, "manual_review_tagger.py").write_text(
    "def to_ife_features(tags, base):\n    return dict(base)\n"
    "def predict_frame_tags(path, ocr, tagger):\n    return ['Map'], False\n")
os.environ["IFE_OCR_REPO"] = _STUB
sys.path.insert(0, str(Path(__file__).parent))

import gather_short_chapters as g      # noqa: E402
import resolve_cache_conflict as rcc   # noqa: E402
import pruned_urls                     # noqa: E402


def video(**kw):
    r = {"media_type": "video", "url": "https://www.youtube.com/watch?v=abcdefghijk", "published_at": "2026-10-20"}
    r.update(kw)
    return r


class TestVersioning(unittest.TestCase):
    def test_unstamped_and_old_rows_are_stale(self):
        self.assertTrue(g._stale({}))
        self.assertTrue(g._stale({"ocr_tagger_v": g.TAGGER_V - 1}))
        self.assertFalse(g._stale({"ocr_tagger_v": g.TAGGER_V}))

    def test_stale_ife_row_is_requeued_but_a_failure_is_not(self):
        chap = [{"sec": 10, "ife": True, "title": "IFE"}]
        base = dict(chapters=chap, chapters_source="description")
        self.assertTrue(g._needs_ife_ocr(video(**base)))
        self.assertTrue(g._needs_ife_ocr(video(ife_tags_status="done", **base)))
        self.assertFalse(g._needs_ife_ocr(video(ife_tags_status="done", ocr_tagger_v=g.TAGGER_V, **base)))
        self.assertFalse(g._needs_ife_ocr(video(ife_tags_status="fail", **base)))

    def test_rebuild_drops_a_stale_false_tag(self):
        r = video(ife_tags=[{"tag": "Map"}], screen_tags=["Map", "Navigation Menu"], screen_tag_source="ocr-ife-chapter")
        self.assertEqual(g._ocr_screen_tags(r), ["Map"])

    def test_rebuild_keeps_tags_from_other_sources(self):
        r = video(ife_tags=[], screen_tags=["Games"], screen_tag_source="auto")
        self.assertEqual(g._ocr_screen_tags(r), ["Games"])


class TestNewVideoSelection(unittest.TestCase):
    def test_kinds(self):
        ife = [{"sec": 10, "ife": True, "title": "IFE"}]
        self.assertTrue(g._never_tried(video()))                                         # no chapters: whole video
        self.assertTrue(g._never_tried(video(chapters=ife, chapters_source="description")))  # IFE chapter
        self.assertFalse(g._never_tried(video(chapters=[{"sec": 1, "title": "Intro"}], chapters_source="description")))
        self.assertFalse(g._never_tried(video(chapters_source="ocr", chapters=[{"sec": 1}])))
        self.assertFalse(g._never_tried(video(chapters_ocr_status="fail")))
        self.assertFalse(g._never_tried(video(chapters=ife, chapters_source="description", ife_tags_status="none")))


class TestResolverKeepsNewerOcr(unittest.TestCase):
    def test_newer_version_survives_losing_the_row(self):
        win = {"url": "u", "ife_tags_status": "done", "ife_tags": [{"tag": "Navigation Menu"}], "ocr_tagger_v": 2}
        lose = {"url": "u", "ife_tags_status": "done", "ife_tags": [{"tag": "Map"}], "ocr_tagger_v": 3}
        rcc._carry_ocr(win, lose)
        self.assertEqual(win["ife_tags"], [{"tag": "Map"}])
        self.assertEqual(win["ocr_tagger_v"], 3)

    def test_older_version_does_not_overwrite(self):
        win = {"url": "u", "ife_tags_status": "done", "ife_tags": [{"tag": "Map"}], "ocr_tagger_v": 3}
        lose = {"url": "u", "ife_tags_status": "done", "ife_tags": [{"tag": "Games"}], "ocr_tagger_v": 2}
        rcc._carry_ocr(win, lose)
        self.assertEqual(win["ife_tags"], [{"tag": "Map"}])


class TestPrunedList(unittest.TestCase):
    def test_listed_internal_row_is_dropped_but_unlisted_one_is_kept(self):
        rows = [{"url": "internal://a", "media_type": "internal"}, {"url": "internal://b", "media_type": "internal"},
                {"url": "https://y/1"}, {"url": "https://y/2"}]
        kept = pruned_urls.drop_pruned(rows, {"internal://a", "https://y/1"})
        self.assertEqual([r["url"] for r in kept], ["internal://b", "https://y/2"])


class TestInternalMedia(unittest.TestCase):
    def setUp(self):
        import tag_internal_media as t
        self.t = t
        self.tmp = Path(tempfile.mkdtemp(prefix="ife_internal_"))
        (self.tmp / "static" / "uploads").mkdir(parents=True)
        (self.tmp / "static" / "uploads" / "p1.jpg").write_bytes(b"x")
        (self.tmp / "static" / "uploads" / "p2.jpg").write_bytes(b"x")
        t.HERE, t.CACHE = self.tmp, self.tmp / "ife_cache.json"
        rows = [{"url": "internal://1", "media_type": "internal", "photos": ["/static/uploads/p1.jpg", "/static/uploads/p2.jpg"],
                 "photo_tags": {"/static/uploads/p1.jpg": {"tags": ["Map"]}}, "images": []}]
        t.CACHE.write_text(json.dumps({"reviews": rows}))

    def args(self):
        return SimpleNamespace(url=None, force=False, limit=0)

    def test_only_untagged_uploaded_photos_are_pending(self):
        _, todo = self.t._pending(self.args())
        self.assertEqual([it["_photo"] for _, it in todo], ["/static/uploads/p2.jpg"])

    def test_force_redoes_all_photos(self):
        _, todo = self.t._pending(SimpleNamespace(url=None, force=True, limit=0))
        self.assertEqual(len(todo), 2)

    def test_second_run_cannot_take_the_lock(self):
        first = self.t._single_instance()
        self.assertIsNotNone(first)
        self.assertIsNone(self.t._single_instance())
        first.close()

    def test_tag_uploaded_stores_per_photo_tags(self):
        r = {"url": "internal://1", "photos": ["/static/uploads/p2.jpg"]}
        ok = self.t.tag_uploaded(r, {"_photo": "/static/uploads/p2.jpg", "local": "/static/uploads/p2.jpg"}, None, None)
        self.assertTrue(ok)
        self.assertEqual(r["photo_tags"]["/static/uploads/p2.jpg"]["tags"], ["Map"])


if __name__ == "__main__":
    unittest.main()
