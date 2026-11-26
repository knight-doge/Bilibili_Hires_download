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
                patch.object(api, "get_playurl", return_value={"dash": DASH}):
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

    def test_preferred_subtitle_skips_unavailable_ai_track(self):
        tracks = [{"lan": "ai-zh", "subtitle_url": ""}, {"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/zh"},
                  {"lan": "en", "subtitle_url": "//aisubtitle.hdslb.com/en"}]
        with tempfile.TemporaryDirectory() as temp, patch.object(subtitles, "get_tracks", return_value=tracks), \
                patch.object(subtitles, "fetch_cues", return_value=[subtitles.Cue(1000, 2000, "字幕")]) as fetch:
            stem = Path(temp) / "音频"
            self.assertEqual(subtitles.export_page(object(), BV1, 4, stem, prefer_chinese=True, log=lambda _: None), 0)
            self.assertEqual(fetch.call_args.args[1], "//aisubtitle.hdslb.com/zh")
            self.assertTrue((Path(temp) / "音频.lrc").exists())
            self.assertEqual(len(list(Path(temp).glob("*.srt"))), 1)
