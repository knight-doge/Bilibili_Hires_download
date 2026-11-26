"""单条链接/多 P 的完整下载流水线。"""
from __future__ import annotations

import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from . import api, auth, formats
from .analyze import analyze, format_report
from .config import DEFAULT_DOWNLOAD_DIR, REFERER, UA, human_size
from .download import download
from .models import DownloadResult, Page, Track, Video
from .remux import probe, remux
from .tag import write_report

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_name(name: str, limit: int = 110) -> str:
    name = _BAD.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:limit] or "untitled"


def clean_stem(name: str) -> str:
    name = "".join(char for char in unicodedata.normalize("NFC", name)
                   if unicodedata.category(char)[0] in "LNM" and unicodedata.category(char) != "Me"
                   and not (0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF))[:110]
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name, re.I):
        name += "音频"
    return name or "untitled"


@dataclass
class Options:
    out: Path = field(default_factory=lambda: DEFAULT_DOWNLOAD_DIR)
    atmos: bool = False
    aac_fallback: bool = True
    verify: bool = True
    tag: bool = True
    threads: int = 4
    pages: str = "all"
    keep_temp: bool = False
    fmt: str = "flac"
    preset: str = "medium"
    audio_priority: tuple[str, ...] = ()
    fallback_to_source: bool = False
    clean_filenames: bool = False


Log = Callable[[str], None]


def select_tracks(dash: dict, opts: Options) -> list[Track]:
    out: list[Track] = []
    flac = (dash.get("flac") or {}).get("audio")
    if flac:
        out.append(
            Track(
                kind="flac",
                id=flac.get("id", 30251),
                codec=flac.get("codecs", "fLaC"),
                bandwidth=flac.get("bandwidth", 0),
                urls=[u for u in [flac.get("baseUrl") or flac.get("base_url")] + (flac.get("backupUrl") or flac.get("backup_url") or []) if u],
                label="Hi-Res 无损 FLAC",
                ext="flac",
            )
        )
    dolby = (dash.get("dolby") or {}).get("audio") or []
    for a in dolby:
        out.append(
            Track(
                kind="dolby",
                id=a.get("id", 30250),
                codec=a.get("codecs", "ec-3"),
                bandwidth=a.get("bandwidth", 0),
                urls=[u for u in [a.get("baseUrl") or a.get("base_url")] + (a.get("backupUrl") or a.get("backup_url") or []) if u],
                label="杜比全景声 E-AC3",
                ext="eac3",
            )
        )
    aac = dash.get("audio") or []
    if opts.audio_priority:
        aac = [a for a in aac if a.get("baseUrl") or a.get("base_url")
               or a.get("backupUrl") or a.get("backup_url")]
    if aac:
        best = max(aac, key=lambda a: a.get("bandwidth", 0))
        out.append(
            Track(
                kind="aac",
                id=best.get("id"),
                codec=best.get("codecs", "mp4a.40.2"),
                bandwidth=best.get("bandwidth", 0),
                urls=[u for u in [best.get("baseUrl") or best.get("base_url")] + (best.get("backupUrl") or best.get("backup_url") or []) if u],
                label=f"AAC {round(best.get('bandwidth', 0) / 1000)}k",
                ext="m4a",
            )
        )
    return out


def choose_tracks(tracks: list[Track], opts: Options) -> tuple[list[Track], str | None]:
    """返回 (要下载的轨道, 提示信息)。"""
    if opts.audio_priority:
        for kind in opts.audio_priority:
            track = next((t for t in tracks if t.kind == kind and t.urls), None)
            if track:
                note = None if kind == opts.audio_priority[0] else f"首选音轨不可用，已降级为 {track.label}"
                return [track], note
        return [], "没有可用音轨"
    flac = next((t for t in tracks if t.kind == "flac"), None)
    dolby = next((t for t in tracks if t.kind == "dolby"), None)
    aac = next((t for t in tracks if t.kind == "aac"), None)

    chosen: list[Track] = []
    note = None
    if flac:
        chosen.append(flac)
    elif aac and opts.aac_fallback:
        chosen.append(aac)
        note = "该视频没有 Hi-Res 无损音轨（UP 主未上传或需要大会员），已回退到最高 AAC"
    else:
        note = "该视频没有可用的 Hi-Res 无损音轨"
    if opts.atmos and dolby:
        chosen.append(dolby)
    elif opts.atmos and not dolby:
        note = (note + "；" if note else "") + "该视频没有杜比全景声音轨"
    return chosen, note


def parse_pages(spec: str, pages: list[Page]) -> list[Page]:
    if spec.lower() == "all":
        return pages
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            wanted.update(page.index for page in pages if start <= page.index <= end)
        elif part:
            wanted.add(int(part))
    return [p for p in pages if p.index in wanted]


def _fetch_cover(url: str) -> bytes | None:
    if not url:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": REFERER}, timeout=15)
        if r.ok and len(r.content) > 1000:
            return r.content
    except Exception:
        pass
    return None


def process_url(session, url: str, opts: Options, log: Log = print, progress=None) -> list[DownloadResult]:
    vid = api.resolve_url(session, url)
    video = api.get_video(session, vid)
    return process_video(session, video, opts, log=log, progress=progress)


def process_video(session, video: Video, opts: Options, log: Log = print, progress=None) -> list[DownloadResult]:
    pages = parse_pages(opts.pages, video.pages)
    log(f"■ {video.title}  ({video.bvid}, {video.owner}, {len(video.pages)}P)")

    out_root = Path(opts.out)
    results: list[DownloadResult] = []
    cover = _fetch_cover(video.cover) if opts.tag else None

    for page in pages:
        dash = api.get_playurl(session, video.bvid, page.cid).get("dash") or {}
        tracks = select_tracks(dash, opts)
        chosen, note = choose_tracks(tracks, opts)
        if note:
            log(f"  ! {note}")
        if not chosen:
            continue

        base_dir = out_root if len(video.pages) == 1 else out_root / safe_name(video.title)
        if opts.clean_filenames:
            stem = clean_stem(video.title) if len(video.pages) == 1 else f"{page.index:02d}{clean_stem(page.title)}"
        else:
            stem = safe_name(video.title) if len(video.pages) == 1 else f"{page.index:02d} {safe_name(page.title)}"

        for track in chosen:
            fmt = opts.fmt
            if opts.fallback_to_source and fmt == "flac" and track.kind != "flac":
                fmt = "source"
                log("  ! 无原生无损音轨，保留有损原轨")
            spec = formats.get_format(fmt)
            tag_txt = "无损" if track.kind == "flac" else ("全景声" if track.kind == "dolby" else "AAC")
            target_ext = track.ext if fmt == "source" else spec.ext.lstrip(".")
            log(f"  ↓ P{page.index} {track.label} [{tag_txt}] {track.kbps} kb/s → {formats.describe(fmt, opts.preset)}")
            file_stem = stem
            if track.kind == "dolby":
                file_stem += "Atmos" if opts.clean_filenames else " [Atmos]"
            final = base_dir / f"{file_stem}.{target_ext}"

            def refresh(_cid=page.cid, _kind=track.kind) -> list[str]:
                d = api.get_playurl(session, video.bvid, _cid).get("dash") or {}
                ts = select_tracks(d, opts)
                t = next((x for x in ts if x.kind == _kind), None)
                return t.urls if t else []

            temp = base_dir / f".{stem}.{track.kind}.m4s"
            try:
                download(
                    track.urls,
                    temp,
                    threads=opts.threads,
                    progress=_progress_logger(log, page, track, progress),
                    refresh=refresh,
                    replace=True,
                )
                if fmt == "source" or (fmt == "flac" and track.kind == "flac"):
                    remux(temp, final, ext=target_ext)
                else:
                    if spec.lossless and track.kind in ("aac", "dolby"):
                        log("  ! 源本身是有损音轨，转成无损格式只是换容器，不会恢复音质")
                    if opts.fallback_to_source:
                        converted = temp.with_name(f"{temp.name}.convert.{target_ext}")
                        try:
                            formats.convert(temp, converted, fmt, opts.preset)
                            converted.replace(final)
                        except RuntimeError:
                            fmt = "source"
                            target_ext = track.ext
                            final = base_dir / f"{file_stem}.{target_ext}"
                            log("  ! 预设格式转换失败，改为保留原始音轨")
                            remux(temp, final, ext=target_ext)
                        finally:
                            converted.unlink(missing_ok=True)
                    else:
                        formats.convert(temp, final, fmt, opts.preset)
            finally:
                if not opts.keep_temp:
                    temp.unlink(missing_ok=True)
                    temp.with_name(temp.name + ".part.json").unlink(missing_ok=True)
                    temp.with_name(temp.name + ".part").unlink(missing_ok=True)

            info = probe(final)
            real_track = Track(
                kind=track.kind,
                id=track.id,
                codec=track.codec,
                bandwidth=track.bandwidth,
                urls=[],
                label=track.label,
                ext=target_ext,
                sample_rate=info.get("sample_rate"),
                channels=info.get("channels"),
            )

            if opts.verify and fmt in ("source", "flac"):
                analysis = analyze(final)
            else:
                analysis = {
                    **info,
                    "is_lossless_codec": track.kind == "flac" if fmt == "source" else spec.lossless,
                    "nyquist": int((info.get("sample_rate") or 0) / 2),
                    "cutoff_hz": None,
                    "cutoff_ratio": None,
                    "steepness": None,
                    "estimated_source_rate": None,
                    "spectrogram": None,
                    "verdict": "CONVERTED",
                    "verdict_text": formats.describe(fmt, opts.preset),
                }

            if opts.tag:
                try:
                    formats.tag_audio(
                        final,
                        title=page.title if len(video.pages) > 1 else video.title,
                        artist=video.owner,
                        album=video.title,
                        track=page.index,
                        total=len(video.pages),
                        comment=f"bilibili {video.bvid} P{page.index} | {track.label} | {formats.describe(fmt, opts.preset)}",
                        cover=cover,
                    )
                except Exception as e:
                    log(f"  ! 写标签失败: {e}")

            if opts.fallback_to_source:
                analysis["output_format"] = fmt
            write_report(final, video, page, real_track, analysis)
            size = final.stat().st_size
            log(f"  ✓ {final.name}  {human_size(size)}")
            if opts.verify:
                for line in format_report(analysis).splitlines():
                    log(line)
            if (fmt in ("source", "flac") and track.kind == "flac"
                    and analysis.get("verdict") in ("TRANSCODE", "LOSSY_CODEC", "UPSAMPLED", "SUSPECT")):
                log("  ⚠ 这条 FLAC 的音质不达标，详见上面的核验结论")

            results.append(
                DownloadResult(
                    video=video,
                    page=page,
                    track=real_track,
                    path=str(final),
                    bytes=size,
                    analysis=analysis,
                )
            )
    return results


def _mmss(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _progress_logger(log: Log, page: Page, track: Track, progress=None):
    state = {"t": 0.0}

    def cb(done: int, total: int, speed: float) -> None:
        nonlocal state
        now = time.time()
        eta = (total - done) / speed if speed > 0 else 0.0
        if progress:
            progress(done, total, speed, eta)
        if now - state["t"] < 2 and done < total:
            return
        state["t"] = now
        pct = done / total * 100 if total else 0
        log(
            f"    P{page.index} {track.kind} {pct:5.1f}%  "
            f"{human_size(done)}/{human_size(total)}  "
            f"{speed / 1048576:.2f} MB/s  剩余 {_mmss(eta)}"
        )

    return cb
