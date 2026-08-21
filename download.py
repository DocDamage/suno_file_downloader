#!/usr/bin/env python3
"""
Suno Liked 곡 WAV 일괄 다운로더.

collect.js 가 만든 suno_liked.json 을 읽어서 cdn1.suno.ai 에서 WAV 를 내려받는다.
CDN 은 인증이 필요 없으므로 토큰 만료 걱정이 없다.

사용법:
    python download.py                       # suno_liked.json 자동 탐색
    python download.py --json C:\\경로\\suno_liked.json
    python download.py --out D:\\music --workers 6
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) suno-download/1.0"
TIMEOUT = 60
RETRIES = 4
_print_lock = threading.Lock()


def say(*args: object) -> None:
    with _print_lock:
        print(*args, flush=True)


# --------------------------------------------------------------------------- #
# 입력 파일 찾기
# --------------------------------------------------------------------------- #
def find_json(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            sys.exit(f"JSON 파일을 찾을 수 없습니다: {p}")
        return p

    candidates: list[Path] = []
    for d in (Path.cwd(), Path.home() / "Downloads", Path.home() / "다운로드"):
        if d.is_dir():
            candidates += sorted(d.glob("suno_liked*.json"))
    if not candidates:
        sys.exit(
            "suno_liked.json 을 찾지 못했습니다.\n"
            "먼저 collect.js 를 suno.com 콘솔에서 실행하거나, --json 으로 경로를 지정하세요."
        )
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    say(f"입력 파일: {newest}")
    return newest


# --------------------------------------------------------------------------- #
# 파일명 정리 (Windows 금지문자 + 예약어 처리)
# --------------------------------------------------------------------------- #
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(title: str, index: int, ext: str) -> str:
    base = _ILLEGAL.sub("_", title).strip().strip(".")
    base = re.sub(r"\s+", " ", base)
    if base.upper() in _RESERVED or not base:
        base = f"track_{index:03d}"
    base = base[:80].rstrip()
    return f"{index:03d} - {base}.{ext}"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def open_url(url: str, method: str = "GET"):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def remote_size(url: str) -> int | None:
    """HEAD 로 존재 여부와 크기 확인. 없으면 None."""
    try:
        with open_url(url, "HEAD") as r:
            length = r.headers.get("Content-Length")
            return int(length) if length else -1  # -1 = 존재하지만 크기 미상
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        return -1  # 애매하면 일단 시도해 본다
    except Exception:
        return -1


def download(url: str, dest: Path, expected: int | None) -> tuple[str, str]:
    """(상태, 메시지) 반환. 상태: ok | skip | fail"""
    if dest.exists():
        if expected is None or expected <= 0 or dest.stat().st_size == expected:
            return "skip", "이미 있음"
        say(f"  크기 불일치, 다시 받습니다: {dest.name}")

    part = dest.with_suffix(dest.suffix + ".part")
    last_err = ""
    for attempt in range(1, RETRIES + 1):
        try:
            with open_url(url) as r, open(part, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
            size = part.stat().st_size
            if size == 0:
                raise OSError("빈 파일")
            if expected and expected > 0 and size != expected:
                raise OSError(f"크기 불일치 {size} != {expected}")
            part.replace(dest)
            return "ok", f"{size / 1_048_576:.1f} MB"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            part.unlink(missing_ok=True)
            if attempt < RETRIES:
                time.sleep(1.5 * attempt)
    return "fail", last_err


# --------------------------------------------------------------------------- #
# 다운로드 기록 (clip id 기준)
# --------------------------------------------------------------------------- #
MANIFEST_NAME = "downloaded.json"


class Manifest:
    """받은 곡을 clip id 로 기록한다.

    파일명이 아니라 id 로 판단하므로, 나중에 파일 이름을 바꾸거나
    Suno 피드에 새 곡이 추가돼 번호가 밀려도 다시 받지 않는다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.entries = raw.get("entries", {})
            except Exception as e:  # noqa: BLE001
                say(f"기록 파일을 읽지 못해 새로 만듭니다 ({e})")

    def has(self, clip_id: str, folder: Path) -> Path | None:
        """기록돼 있고 파일이 실제로 남아 있으면 그 경로를 반환."""
        rec = self.entries.get(clip_id)
        if not rec:
            return None
        p = folder / rec["file"]
        return p if p.is_file() and p.stat().st_size > 0 else None

    def add(self, clip_id: str, title: str, dest: Path) -> None:
        with self.lock:
            self.entries[clip_id] = {
                "file": dest.name,
                "title": title,
                "size": dest.stat().st_size,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

    def save(self) -> None:
        with self.lock:
            payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "entries": self.entries}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Suno Liked 곡 일괄 다운로드 (WAV / MP3)")
    ap.add_argument("--json", help="collect.js 가 만든 suno_liked.json 경로")
    ap.add_argument(
        "--format", choices=("wav", "mp3"), default="wav", help="받을 포맷 (기본: wav)"
    )
    ap.add_argument("--out", help="저장 폴더 (기본: ./<format>)")
    ap.add_argument("--workers", type=int, default=4, help="동시 다운로드 수 (기본: 4)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="실제로 받지 않고 존재 여부와 총 용량만 확인",
    )
    ap.add_argument(
        "--skip-untitled",
        action="store_true",
        help="제목 없는 곡(untitled)은 건너뛴다",
    )
    args = ap.parse_args()

    data = json.loads(find_json(args.json).read_text(encoding="utf-8"))
    songs = data.get("songs") or []
    if not songs:
        sys.exit("JSON 안에 곡이 없습니다.")

    fmt = args.format
    url_key = f"{fmt}_url"
    label = fmt.upper()

    out = Path(args.out or fmt).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    say(f"저장 폴더: {out.resolve()}")
    say(f"대상: Liked {len(songs)}곡 — {label} 존재 여부 확인 중...\n")

    # 1) 이미 받은 곡 걸러내기 (clip id 기준)
    manifest = Manifest(out / MANIFEST_NAME)
    already = 0
    pending: list[tuple[dict, Path]] = []

    skipped_untitled = 0

    # 번호(i)는 항상 전체 목록 기준이라, 무엇을 걸러내든 파일명이 흔들리지 않는다
    for i, song in enumerate(songs, start=1):
        if args.skip_untitled and song["title"].strip().lower() == "untitled":
            skipped_untitled += 1
            continue
        dest = out / safe_name(song["title"], i, fmt)
        if manifest.has(song["id"], out):
            already += 1
            continue
        if dest.is_file() and dest.stat().st_size > 0:
            # 기록에는 없지만 파일은 있는 경우 → 기록만 채워 넣는다
            manifest.add(song["id"], song["title"], dest)
            already += 1
            continue
        pending.append((song, dest))

    if skipped_untitled:
        say(f"untitled 제외 : {skipped_untitled}곡")
    if already:
        manifest.save()
        say(f"이미 받음  : {already}곡 (건너뜀)")
    if not pending:
        say(f"\n새로 받을 {label} 가 없습니다. 전부 최신 상태입니다.")
        return 0

    # 2) 남은 곡만 서버에 존재 확인
    say(f"확인 필요  : {len(pending)}곡\n")
    jobs: list[tuple[dict, Path, int | None]] = []
    missing: list[dict] = []
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        sizes = list(pool.map(lambda p: remote_size(p[0][url_key]), pending))

    for (song, dest), size in zip(pending, sizes):
        if size is None:
            missing.append(song)
        else:
            jobs.append((song, dest, size))

    if missing:
        miss_file = Path(f"needs_{fmt}.json")
        say(f"⚠ {label} 가 아직 없는 곡 {len(missing)}개.")
        miss_file.write_text(
            json.dumps({"songs": missing}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        say(f"  목록을 {miss_file} 에 저장했습니다.\n")

    if not jobs:
        say(f"지금 받을 수 있는 {label} 가 없습니다.")
        return 1

    # 2-b) 용량 요약
    known = [s for _, _, s in jobs if s and s > 0]
    unknown = len(jobs) - len(known)
    total = sum(known)

    say(f"{label} 받을 곡  : {len(jobs)}곡")
    say(f"예상 용량     : {total / 2**30:.1f} GB" + (f" (+ 크기 미상 {unknown}곡)" if unknown else ""))

    free_bytes = shutil.disk_usage(out).free
    say(f"디스크 여유   : {free_bytes / 2**30:.1f} GB")
    if free_bytes < total * 1.05:
        say("⚠ 디스크 공간이 부족할 수 있습니다.")

    if args.dry_run:
        say("\n--dry-run 이라 여기서 멈춥니다. 실제로 받으려면 --dry-run 을 빼고 다시 실행하세요.")
        return 0

    # 3) 다운로드
    say(f"{label} {len(jobs)}곡 다운로드 시작 (동시 {args.workers}개)\n")
    tally = {"ok": 0, "skip": 0, "fail": 0}
    failures: list[str] = []
    done = 0

    def run(job):
        song, dest, size = job
        status, msg = download(song[url_key], dest, size)
        return song, dest, status, msg

    try:
        with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for song, dest, status, msg in pool.map(run, jobs):
                done += 1
                tally[status] += 1
                icon = {"ok": "✓", "skip": "-", "fail": "✗"}[status]
                say(f"[{done}/{len(jobs)}] {icon} {dest.name}  ({msg})")
                if status in ("ok", "skip"):
                    manifest.add(song["id"], song["title"], dest)
                    if done % 20 == 0:
                        manifest.save()
                else:
                    failures.append(f"{song['title']} ({song['id']}): {msg}")
    finally:
        manifest.save()  # 중간에 끊겨도 여기까지는 기록에 남는다

    say(f"\n완료 — 받음 {tally['ok']}, 건너뜀 {tally['skip']}, 실패 {tally['fail']}")
    say(f"기록 파일 — {manifest.path} (총 {len(manifest.entries)}곡)")
    if failures:
        say("\n실패 목록:")
        for f in failures:
            say("  " + f)
    if missing:
        say(f"\n{label} 미생성 {len(missing)}곡은 needs_{fmt}.json 참고")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
