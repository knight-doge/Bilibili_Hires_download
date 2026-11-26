"""批量队列、合集分页与下载结果报告。"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import api, subtitles
from .config import find_ffmpeg, hidden_kwargs
from .http import api_get
from .models import Page, Video
from .pipeline import Options, choose_tracks, parse_pages, process_video, select_tracks


PRESETS = {
    "原始音轨 · Hi-Res 优先": ("source", ("flac", "aac", "dolby")),
    "FLAC 优先 · 缺失保留原轨": ("flac", ("flac", "aac", "dolby")),
    "原始音轨 · 杜比优先": ("source", ("dolby", "flac", "aac")),
    "通用 MP3": ("mp3", ("flac", "aac", "dolby")),
    "iPod 兼容": ("ipod", ("flac", "aac", "dolby")),
    "ALAC": ("alac", ("flac", "aac", "dolby")),
    "WAV": ("wav", ("flac", "aac", "dolby")),
    "AAC": ("aac", ("aac", "flac", "dolby")),
}


@dataclass
class Entry:
    video: Video
    pages: list[Page]
    preview: str


@dataclass
class Queue:
    entries: list[Entry]
    rejected: list[dict]
    caption_labels: dict[str, str] = field(default_factory=dict)
    captions_by_part: dict[tuple[str, int], set[str]] = field(default_factory=dict)

    @property
    def part_count(self) -> int:
        return sum(len(entry.pages) for entry in self.entries)

    @property
    def unknown_captions(self) -> int:
        return self.part_count - len(self.captions_by_part)

    def caption_counts(self) -> Counter:
        return Counter(kind for kinds in self.captions_by_part.values() for kind in kinds)


def collection_ids(session, source: str) -> list[str] | None:
    url = urlsplit(source)
    if url.scheme not in ("http", "https") or url.hostname != "space.bilibili.com":
        return None
    match = re.fullmatch(r"/(\d+)/lists/(\d+)/?", url.path)
    if not match:
        return None
    if parse_qs(url.query).get("type", ["season"])[0] != "season":
        raise ValueError("仅支持空间中的合集（season）链接")
    mid, season_id = match.groups()
    ids = []
    total = None
    page = 1
    while total is None or len(ids) < total:
        data = api_get(session, "/x/polymer/web-space/seasons_archives_list", {
            "mid": mid, "season_id": season_id, "page_num": page, "page_size": 30,
        })["data"]
        current = int(data["page"]["total"])
        if total is not None and total != current:
            raise ValueError("合集数量在分页期间变化，请重试")
        total = current
        chunk = [archive.get("bvid", "") for archive in data.get("archives", [])]
        if not chunk and len(ids) < total:
            raise ValueError("合集分页返回不完整")
        ids.extend(chunk)
        page += 1
    if len(ids) != total or len(set(ids)) != total or not all(api.BV_RE.fullmatch(vid) for vid in ids):
        raise ValueError("合集清单数量或 BV 号校验失败")
    return ids


def prepare_queue(session, sources: str, opts: Options, *, log, progress, stopped) -> Queue:
    candidates = {}
    rejected = []
    for source in dict.fromkeys(line.strip() for line in sources.splitlines() if line.strip()):
        if stopped():
            break
        try:
            ids = collection_ids(session, source)
            if ids is not None:
                log(f"合集已展开：{len(ids)} 条")
            for vid in ids if ids is not None else [api.resolve_url(session, source)]:
                candidates.setdefault(vid, None)
        except Exception as error:
            rejected.append({"source": source, "error": str(error)})
            log(f"✗ 无法解析 {source}：{error}")
    entries = {}
    for index, vid in enumerate(candidates, 1):
        if stopped():
            break
        try:
            video = api.get_video(session, vid)
            if video.bvid in entries:
                continue
            pages = parse_pages(opts.pages, video.pages)
            if not pages:
                raise ValueError("没有匹配的分 P")
            page = pages[0]
            try:
                dash = api.get_playurl(session, video.bvid, page.cid).get("dash") or {}
                chosen, _ = choose_tracks(select_tracks(dash, opts), opts)
                preview = f"P{page.index} · {chosen[0].label if chosen else '无可用音轨'}"
            except Exception as error:
                preview = f"P{page.index} · 下载时重试"
                log(f"! {video.bvid} 音轨预览失败：{error}")
            entries[video.bvid] = Entry(video, pages, preview)
        except Exception as error:
            rejected.append({"source": vid, "error": str(error)})
            log(f"✗ 无法解析 {vid}：{error}")
        finally:
            progress(index, len(candidates), vid, "解析中")
    queue = Queue(list(entries.values()), rejected)
    total = queue.part_count
    index = 0
    for entry in queue.entries:
        for page in entry.pages:
            if stopped():
                return queue
            bvid = entry.video.bvid
            try:
                kinds = set()
                for track in subtitles.get_tracks(session, bvid, page.cid):
                    if track.get("subtitle_url"):
                        kind = subtitles.track_type(track)
                        queue.caption_labels.setdefault(kind, subtitles.type_label(track))
                        kinds.add(kind)
                queue.captions_by_part[bvid, page.index] = kinds
            except Exception as error:
                log(f"! {bvid} P{page.index} 字幕未确认：{error}")
            index += 1
            progress(index, total, bvid, "读取字幕")
    return queue


def _download_page(session, root, video, page, opts, ffmpeg, caption_mode, caption_types, log) -> dict:
    page_opts = replace(opts, out=root / video.bvid, pages=str(page.index))
    results = process_video(session, video, page_opts, log=log)
    if len(results) != 1:
        raise RuntimeError("没有拿到可用音轨")
    result = results[0]
    audio = Path(result.path)
    if ffmpeg:
        check = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(audio),
                                "-map", "0:a:0", "-f", "null", "-"],
                               capture_output=True, timeout=180, **hidden_kwargs())
        if check.returncode:
            raise RuntimeError("FFmpeg 完整解码失败")
    captions = []
    caption_errors = 0
    if caption_types:
        stem = audio.with_suffix("")
        try:
            saved, caption_errors = subtitles.export_selected(
                session, video.bvid, page.cid, stem, caption_types,
                first_only=caption_mode == "priority", log=log,
            )
            captions = [{"type": track["type"], "srt": str(track["srt"].relative_to(root)),
                         "lrc": str(track["lrc"].relative_to(root))} for track in saved]
        except Exception as error:
            log(f"! 字幕导出失败：{error}")
            caption_errors = 1
        if not captions and not caption_errors:
            log("  ! 无勾选类型的可用字幕，保留音频")
    status = "off" if not caption_types else ("saved" if captions else "failed" if caption_errors else "missing")
    return {"page": page.index, "ok": True, "track_id": result.track.id,
            "source": result.track.kind, "codec": result.analysis.get("codec"),
            "audio_fallback": bool(opts.audio_priority and result.track.kind != opts.audio_priority[0]),
            "output_format": result.analysis.get("output_format", opts.fmt),
            "format_fallback": result.analysis.get("output_format", opts.fmt) != opts.fmt,
            "path": str(audio.relative_to(root)), "bytes": result.bytes,
            "decoded": bool(ffmpeg), "subtitles": [track[key] for track in captions for key in ("srt", "lrc")],
            "subtitle_tracks": captions, "subtitle_status": status, "subtitle_errors": caption_errors}


def caption_coverage(queue: Queue, types: tuple[str, ...], records) -> dict:
    counts = Counter()
    parts = 0
    for record in records:
        kinds = {track["type"] for track in record.get("subtitle_tracks", []) if track.get("lrc")}
        kinds.intersection_update(types)
        if kinds:
            parts += 1
            counts.update(kinds)
    missing = [kind for kind in types if not counts[kind]]
    return {"total_parts": queue.part_count, "parts_with_lyrics": parts,
            "unconfirmed_parts": queue.unknown_captions, "complete": bool(types) and not missing,
            "missing_types": missing, "saved_by_type": {kind: counts[kind] for kind in types}}


def download_queue(session, queue: Queue, opts: Options, *, preset: str, caption_mode: str,
                   verify: bool, log, progress, stopped, caption_types: tuple[str, ...] = (),
                   captions_progress=None) -> tuple[Path, dict]:
    decoder = find_ffmpeg()
    ffmpeg = decoder if verify else None
    opts.out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("batch-%Y%m%d-%H%M%S-"), dir=opts.out))
    log(f"输出目录：{root}")
    tasks = [(entry.video, page) for entry in queue.entries for page in entry.pages]
    records = {}
    retry = []
    total = len(tasks)
    try:
        with (root / "progress.jsonl").open("w", encoding="utf-8") as stream:
            for attempt, work in ((1, tasks), (2, retry)):
                for index, (video, page) in enumerate(work, 1):
                    if stopped():
                        break
                    log(f"{video.bvid} P{page.index} · {'下载' if attempt == 1 else '重试'}")
                    try:
                        record = _download_page(session, root, video, page, opts, ffmpeg, caption_mode, caption_types, log)
                    except Exception as error:
                        record = {"page": page.index, "ok": False, "error": str(error)}
                        log(f"✗ {video.bvid} P{page.index}：{error}")
                        if attempt == 1:
                            retry.append((video, page))
                    records[(video.bvid, page.index)] = record
                    stream.write(json.dumps({"bvid": video.bvid, "attempt": attempt, "subtitle_mode": caption_mode,
                                             "selected_subtitle_types": caption_types, **record}, ensure_ascii=False) + "\n")
                    stream.flush()
                    progress(index if attempt == 1 else total, total, video.bvid,
                             f"P{page.index} {'完成' if record['ok'] else '失败'}")
                    if captions_progress and caption_types:
                        captions_progress(caption_coverage(queue, caption_types, records.values()))
    finally:
        videos = []
        for entry in queue.entries:
            pages = [records.get((entry.video.bvid, page.index), {"page": page.index, "ok": False, "error": "未执行"})
                     for page in entry.pages]
            videos.append({"bvid": entry.video.bvid, "title": entry.video.title, "pages": pages,
                           "ok": all(page["ok"] for page in pages)})
        completed = [page for video in videos for page in video["pages"] if page["ok"]]
        summary = {"preset": preset, "subtitle_mode": caption_mode, "stopped": stopped(),
                   "selected_subtitle_types": caption_types, "subtitle_type_labels": queue.caption_labels,
                   "subtitle_availability": dict(queue.caption_counts()),
                   "subtitle_coverage": caption_coverage(queue, caption_types, records.values()),
                   "requested_format": opts.fmt, "encoding_preset": opts.preset,
                   "audio_priority": opts.audio_priority,
                   "videos_requested": len(queue.entries), "videos_complete": sum(video["ok"] for video in videos),
                   "tracks_saved": len(completed), "tracks_verified": sum(page["decoded"] for page in completed),
                   "incomplete": [video["bvid"] for video in videos if not video["ok"]],
                   "parse_errors": queue.rejected, "videos": videos}
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return root, summary
