import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bili_audio import api, batch, pipeline, subtitles
from bili_audio.models import DownloadResult, Page, Track, Video


BV1 = "BV1GJ4m1g7Sk"
BV2 = "BV1S1421d74g"
COLLECTION = "https://space.bilibili.com/10/lists/20?type=season"
DASH = {"audio": [{"id": 1, "bandwidth": 300000},
                  {"id": 30280, "bandwidth": 192000, "baseUrl": "https://example.org/audio"}]}


def video(vid, *pages):
    return Video(vid, 1, "歌 名！🎵", "作者", "", list(pages))


def page_options(root):
    return pipeline.Options(out=Path(root), fmt="flac", audio_priority=("flac", "aac", "dolby"),
                            fallback_to_source=True, clean_filenames=True, tag=False, verify=False)


def callbacks():
    return {"log": lambda _msg: None, "progress": lambda *_args: None, "stopped": lambda: False}


class BatchTests(unittest.TestCase):
    def test_expands_collection_and_deduplicates_requests(self):
        def payload(vid):
            return {"data": {"page": {"total": 2}, "archives": [{"bvid": vid}]}}

        records = {BV1: video(BV1, Page(1, 4, "第一 P")), BV2: video(BV2, Page(1, 5, "第一 P"))}
        with patch.object(batch, "api_get", side_effect=[payload(BV1), payload(BV2)]) as get, \
                patch.object(api, "get_video", side_effect=lambda _s, vid: records[vid]) as videos, \
                patch.object(api, "get_playurl", return_value={"dash": DASH}), \
                patch.object(subtitles, "get_tracks", return_value=[]):
            queue = batch.prepare_queue(object(), f"{COLLECTION}\nhttps://www.bilibili.com/video/{BV1}\n错误链接",
                                        page_options("."), **callbacks())
        self.assertEqual([entry.video.bvid for entry in queue.entries], [BV1, BV2])
        self.assertEqual([call.args[2]["page_num"] for call in get.call_args_list], [1, 2])
        self.assertEqual(videos.call_count, 2)
        self.assertEqual(len(queue.rejected), 1)
        self.assertIn("AAC", queue.entries[0].preview)

    def test_continues_after_failed_part_and_retries_only_that_part(self):
        movie = video(BV1, Page(1, 4, "一"), Page(2, 5, "二"))
        queue = batch.Queue([batch.Entry(movie, movie.pages, "AAC")], [{"source": "bad", "error": "invalid"}])
        calls = []

        def process(_session, current, opts, *, log):
            number = int(opts.pages)
            calls.append(number)
            if calls == [1]:
                raise RuntimeError("temporary failure")
            opts.out.mkdir(parents=True, exist_ok=True)
            file = opts.out / f"{number}.m4a"
            file.write_bytes(b"audio")
            track = Track("aac", 30280, "aac", 192000, [], "AAC", "m4a")
            return [DownloadResult(current, movie.pages[number - 1], track, str(file), 5,
                                   {"codec": "aac", "output_format": "source"})]

        with tempfile.TemporaryDirectory() as temp, patch.object(batch, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(batch, "process_video", side_effect=process), \
                patch.object(batch.subprocess, "run") as decoder:
            decoder.return_value.returncode = 0
            root, result = batch.download_queue(object(), queue, page_options(temp), preset="FLAC", caption_mode="none",
                                                verify=True, **callbacks())
            self.assertEqual(calls, [1, 2, 1])
            self.assertEqual(decoder.call_count, 2)
            self.assertEqual(result["tracks_verified"], 2)
            self.assertEqual(result["incomplete"], [])
            self.assertTrue(result["videos"][0]["pages"][0]["format_fallback"])
            self.assertEqual(json.loads((root / "summary.json").read_text(encoding="utf-8"))["parse_errors"], queue.rejected)
            self.assertEqual(len((root / "progress.jsonl").read_text(encoding="utf-8").splitlines()), 3)

    def test_missing_flac_and_conversion_failure_keep_original_aac(self):
        movie = video(BV1, Page(1, 4, "第一页"))

        def download(_urls, temp, **_kwargs):
            temp.parent.mkdir(parents=True, exist_ok=True)
            temp.write_bytes(b"audio")

        def remux(_src, dst, **_kwargs):
            dst.write_bytes(b"audio")

        for target in ("flac", "mp3"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp, \
                    patch.object(api, "get_playurl", return_value={"dash": DASH}), \
                    patch.object(pipeline, "download", side_effect=download), \
                    patch.object(pipeline, "remux", side_effect=remux), \
                    patch.object(pipeline.formats, "convert", side_effect=RuntimeError("encoder unavailable")) as convert, \
                    patch.object(pipeline, "probe", return_value={"codec": "aac"}):
                opts = page_options(temp)
                opts.fmt = target
                results = pipeline.process_video(object(), movie, opts, log=lambda _msg: None)
                self.assertEqual(convert.call_count, int(target == "mp3"))
                self.assertEqual(results[0].track.id, 30280)
                self.assertEqual(results[0].analysis["output_format"], "source")
                self.assertEqual(Path(results[0].path).name, "歌名.m4a")
                self.assertFalse(any(Path(temp).glob(".*.convert.*")))

    def test_availability_and_coverage_count_parts_once_per_type(self):
        movie = video(BV1, *(Page(index, index, str(index)) for index in range(1, 19)))

        def tracks(_session, _bvid, cid):
            if cid == 18:
                raise RuntimeError("lookup failed")
            if cid <= 10:
                return [{"lan": "zh-CN", "subtitle_url": "zh"}, {"lan": "zh-CN", "subtitle_url": "zh-alt"}]
            return [{"lan": "ai-zh", "subtitle_url": "ai"}, {"lan": "en", "subtitle_url": ""}]

        with patch.object(api, "get_video", return_value=movie), \
                patch.object(api, "get_playurl", return_value={"dash": DASH}), \
                patch.object(subtitles, "get_tracks", side_effect=tracks):
            queue = batch.prepare_queue(object(), BV1, page_options("."), **callbacks())

        self.assertEqual(queue.part_count, 18)
        self.assertEqual(queue.caption_counts(), {"zh-cn": 10, "ai-zh": 7})
        self.assertEqual(queue.unknown_captions, 1)
        checked = ("zh-cn", "ai-zh")
        zh = {"subtitle_tracks": [{"type": "zh-cn", "lrc": "a.lrc"}, {"type": "zh-cn", "lrc": "alt.lrc"}]}
        ai_srt = {"subtitle_tracks": [{"type": "ai-zh", "srt": "b.srt"}]}
        ai = {"subtitle_tracks": [{"type": "ai-zh", "lrc": "b.lrc"}]}
        initial = batch.caption_coverage(queue, checked, [zh, ai_srt])
        self.assertFalse(initial["complete"])
        self.assertEqual(initial["missing_types"], ["ai-zh"])
        final = batch.caption_coverage(queue, checked, [zh, ai])
        self.assertTrue(final["complete"])
        self.assertEqual(final["parts_with_lyrics"], 2)
        self.assertEqual(final["saved_by_type"], {"zh-cn": 1, "ai-zh": 1})

    def test_priority_falls_back_after_failed_caption(self):
        tracks = [{"lan": "zh-CN", "subtitle_url": "zh"}, {"lan": "ai-zh", "subtitle_url": ""},
                  {"lan": "ai-zh", "subtitle_url": "ai"}, {"lan": "en", "subtitle_url": "en"},
                  {"lan": "zh-CN", "subtitle_url": "zh-alt"}]
        with tempfile.TemporaryDirectory() as temp, patch.object(subtitles, "get_tracks", return_value=tracks), \
                patch.object(subtitles, "fetch_cues",
                             side_effect=[OSError("CDN"), [subtitles.Cue(1000, 2000, "字幕")]]) as fetch:
            stem = Path(temp) / "音频"
            saved, failed = subtitles.export_selected(object(), BV1, 4, stem, ("ai-zh", "zh-cn"), first_only=True,
                                                      log=lambda _: None)
            self.assertEqual([call.args[1] for call in fetch.call_args_list], ["ai", "zh"])
            self.assertEqual(failed, 1)
            self.assertEqual([track["type"] for track in saved], ["zh-cn"])
            self.assertTrue((Path(temp) / "音频.lrc").exists())
            self.assertEqual(len(list(Path(temp).glob("*.srt"))), 1)

    def test_all_saves_multiple_tracks_for_selected_type(self):
        tracks = [{"lan": "zh-CN", "subtitle_url": "zh"}, {"lan": "zh-CN", "subtitle_url": "zh-alt"},
                  {"lan": "ai-zh", "subtitle_url": "ai"}]
        with tempfile.TemporaryDirectory() as temp, patch.object(subtitles, "get_tracks", return_value=tracks), \
                patch.object(subtitles, "fetch_cues", return_value=[subtitles.Cue(1000, 2000, "字幕")]) as fetch:
            saved, failed = subtitles.export_selected(object(), BV1, 4, Path(temp) / "音频", ("zh-cn",),
                                                      first_only=False, log=lambda _: None)
            self.assertEqual(failed, 0)
            self.assertEqual([call.args[1] for call in fetch.call_args_list], ["zh", "zh-alt"])
            self.assertEqual([track["type"] for track in saved], ["zh-cn", "zh-cn"])
            for track in saved:
                self.assertIn("00:00:01,000", track["srt"].read_text(encoding="utf-8"))
                self.assertEqual(track["lrc"].read_text(encoding="utf-8"), "[00:01.00]字幕\n")
