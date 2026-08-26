#!/usr/bin/env python3
"""
Suno 라이브러리 다운로더 — 로그인부터 선택 다운로드까지 한 프로그램에서.

    python suno.py            로그인 확인 -> 목록 동기화 -> 웹 UI 열기
    python suno.py login      브라우저를 띄워 로그인만 (최초 1회)
    python suno.py sync       목록만 새로 가져오기
    python suno.py ui         저장된 목록으로 웹 UI만 열기
    python suno.py get --all  UI 없이 전부 받기

로그인 세션은 .chrome-profile/ (또는 .msedge-profile/) 에 남으므로 다음부터는 자동이다.
Edge 를 쓰려면:  suno.py login --browser msedge
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import download as dl  # safe_name / remote_size / download / Manifest 재사용

def _init_console() -> None:
    """콘솔을 UTF-8 로 맞춘다.

    exe 로 묶으면 stdout 이 시스템 코드페이지(한국어 Windows 는 cp949)가 되어
    한글이 깨지고, '—' 같은 문자에서는 UnicodeEncodeError 로 죽는다.
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _dirs() -> tuple[Path, Path]:
    """(번들 자원 위치, 데이터 저장 위치).

    exe 로 묶으면 자원은 임시 추출 폴더(_MEIPASS)에, 저장물은 exe 옆에 둔다.
    """
    if getattr(sys, "frozen", False):
        assets = Path(getattr(sys, "_MEIPASS", "."))
        data = Path(sys.executable).resolve().parent
    else:
        assets = data = Path(__file__).resolve().parent
    # 저장 위치를 옮기고 싶으면 SUNO_DATA_DIR 환경변수로 지정
    override = os.environ.get("SUNO_DATA_DIR")
    if override:
        data = Path(override).expanduser().resolve()
        data.mkdir(parents=True, exist_ok=True)
    return assets, data


ASSETS, HERE = _dirs()
LIBRARY = HERE / "library.json"
SETTINGS = HERE / "settings.json"
BROWSERS = {"chrome": "Chrome", "msedge": "Edge"}
UI_FILE = ASSETS / "ui.html"
# 인증(Clerk)만 있으면 되므로 라이브러리 페이지 대신 가벼운 최상위 페이지를 연다
SUNO_URL = "https://suno.com/"
# cmd_sync 가 "로그인이 안 돼 있다" 를 알리는 종료 코드 (다른 실패와 구분)
NEED_LOGIN = 2

# --------------------------------------------------------------------------- #
# 브라우저에서 실행할 목록 수집 스크립트 (collect.js 와 같은 로직)
# --------------------------------------------------------------------------- #
COLLECT_JS = r"""
async () => {
  const API = 'https://studio-api.prod.suno.com';
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const token = () => window.Clerk.session.getToken();

  async function apiGet(path) {
    let lastErr;
    for (let a = 0; a < 5; a++) {
      try {
        const res = await fetch(API + path, {
          headers: { Authorization: 'Bearer ' + (await token()) },
          credentials: 'include',
        });
        if (res.ok) return await res.json();
        if (res.status === 401 || res.status === 429 || res.status >= 500) {
          lastErr = new Error(res.status + ''); await sleep(700 * (a + 1)); continue;
        }
        throw new Error(res.status + ' @ ' + path);
      } catch (e) { lastErr = e; await sleep(700 * (a + 1)); }
    }
    throw lastErr;
  }

  const asClips = (d) => Array.isArray(d) ? d
    : Array.isArray(d && d.clips) ? d.clips
    : Array.isArray(d && d.results) ? d.results : [];

  // 서버측 liked 필터가 되는지 확인
  let likedParam = null;
  for (const p of ['is_liked=true', 'liked=true']) {
    try {
      const probe = asClips(await apiGet('/api/feed/v2?' + p + '&page=0'));
      if (probe.length && probe.every(c => c.is_liked === true)) { likedParam = p; break; }
    } catch (e) {}
  }

  const seen = new Map();
  let emptyStreak = 0;
  for (let page = 0; page < 1000; page++) {
    const clips = asClips(await apiGet('/api/feed/v2?page=' + page + (likedParam ? '&' + likedParam : '')));
    if (!clips.length) break;
    let added = 0;
    for (const c of clips) {
      if (c && c.id && !seen.has(c.id)) { seen.set(c.id, c); added++; }
    }
    emptyStreak = added === 0 ? emptyStreak + 1 : 0;
    if (emptyStreak >= 2) break;
    await sleep(120);
  }

  const liked = [...seen.values()].filter(c => c.is_liked === true);
  return {
    scanned_total: seen.size,
    server_side_filter: likedParam,
    songs: liked.map(c => ({
      id: c.id,
      title: (c.title || '').trim() || 'untitled',
      created_at: c.created_at || null,
      duration: (c.metadata && c.metadata.duration) || null,
      tags: (c.metadata && c.metadata.tags) || null,
      model: c.model_name || c.major_model_version || null,
      image_url: c.image_url || ('https://cdn2.suno.ai/image_' + c.id + '.jpeg'),
      wav_url: 'https://cdn1.suno.ai/' + c.id + '.wav',
      mp3_url: c.audio_url || ('https://cdn1.suno.ai/' + c.id + '.mp3'),
    })),
  };
}
"""


# --------------------------------------------------------------------------- #
# 브라우저 세션
# --------------------------------------------------------------------------- #
def _settings() -> dict:
    if SETTINGS.is_file():
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def _channel(explicit: str | None = None) -> str:
    """쓸 브라우저. 인자 > 환경변수 > 저장된 설정 > 기본(chrome) 순."""
    ch = explicit or os.environ.get("SUNO_BROWSER") or _settings().get("browser") or "chrome"
    return ch if ch in BROWSERS else "chrome"


def _remember_browser(channel: str) -> None:
    """로그인에 성공한 브라우저를 기억해 다음 실행부터 자동으로 쓴다."""
    cfg = _settings()
    if cfg.get("browser") != channel:
        cfg["browser"] = channel
        SETTINGS.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def _profile_dir(channel: str) -> Path:
    # 브라우저마다 프로필이 호환되지 않으므로 따로 둔다
    return HERE / (".chrome-profile" if channel == "chrome" else f".{channel}-profile")


# 직접 띄운 브라우저를 (프로세스, Playwright Browser) 로 기억해 둔다.
# 종료할 때 Browser.close() 로 정상 종료시켜야 쿠키·세션이 디스크에 남는다.
_SPAWNED: list[tuple[subprocess.Popen, object]] = []

# Playwright 가 브라우저에 붙는 기본 방식(--remote-debugging-pipe)이 통하지 않는
# PC 가 있다. 그럴 때는 우리가 직접 --remote-debugging-port 로 띄우고 붙는다.
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--disable-sync",
    "--disable-features=Translate,OptimizationHints",
]


def _browser_exe(channel: str) -> str | None:
    """설치된 Chrome / Edge 의 실행 파일 경로."""
    exe = "chrome.exe" if channel == "chrome" else "msedge.exe"

    if os.name == "nt":  # 레지스트리의 App Paths 가 가장 정확하다
        try:
            import winreg

            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    key = winreg.OpenKey(
                        root,
                        rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}",
                    )
                    with key:
                        path = winreg.QueryValue(key, None)
                    if path and Path(path).exists():
                        return path
                except OSError:
                    continue
        except Exception:  # noqa: BLE001
            pass

    sub = ("Google/Chrome/Application" if channel == "chrome"
           else "Microsoft/Edge/Application")
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            cand = Path(base) / sub / exe
            if cand.exists():
                return str(cand)

    return shutil.which(exe)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_cdp(port: int, proc: subprocess.Popen, timeout: float = 40.0) -> bool:
    """브라우저의 디버깅 포트가 열릴 때까지 기다린다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1
            ):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def _browser_via_cdp(pw, headless: bool, channel: str):
    """브라우저를 직접 띄운 뒤 CDP 로 붙는다. 실패하면 None."""
    exe = _browser_exe(channel)
    if not exe:
        return None

    port = _free_port()
    profile = _profile_dir(channel)
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        *_LAUNCH_ARGS,
    ]
    if headless:
        args.append("--headless=new")
    else:
        args.append("--window-size=1280,900")
    args.append("about:blank")

    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception:  # noqa: BLE001
        return None

    if not _wait_cdp(port, proc):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return None

    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return None

    _SPAWNED.append((proc, browser))
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.set_viewport_size({"width": 1280, "height": 900})
    except Exception:  # noqa: BLE001
        pass
    return ctx, page


def _browser(headless: bool, channel: str = "chrome"):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright 가 없습니다.  pip install playwright  로 설치하세요.")

    pw = sync_playwright().start()

    # 1순위: Playwright 가 알아서 띄우는 방식
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(_profile_dir(channel)),
            channel=channel,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return pw, ctx, page
    except Exception as e:  # noqa: BLE001
        first_error = e

    # 2순위: 직접 띄우고 CDP 로 붙기
    got = _browser_via_cdp(pw, headless, channel)
    if got is not None:
        ctx, page = got
        return pw, ctx, page

    pw.stop()
    sys.exit(f"{BROWSERS[channel]} 를 띄우지 못했습니다: {first_error}")


def _shutdown(pw, ctx) -> None:
    """브라우저와 Playwright 를 정리한다. 어떤 방식으로 띄웠든 안전하게.

    직접 띄운 브라우저는 반드시 '정상 종료'를 시켜야 한다. 강제로 죽이면
    Chrome 이 쿠키·로컬스토리지를 디스크에 쓰기 전에 끝나서 로그인 세션이
    통째로 날아간다. 다음 실행이 프로필 잠금과 부딪히지 않도록 프로세스가
    완전히 끝날 때까지 기다린다.
    """
    if _SPAWNED:
        while _SPAWNED:
            proc, browser = _SPAWNED.pop()
            try:
                browser.close()  # CDP Browser.close - 세션을 저장하고 끝난다
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
    else:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        pw.stop()
    except Exception:  # noqa: BLE001
        pass


def _goto_suno(page, attempts: int = 5) -> bool:
    """suno.com 을 연다.

    첫 시도는 브라우저가 막 뜬 직후라 자주 늦다. 길게 한 번 기다리면 화면이
    about:blank 인 채로 멈춰 보이므로, 짧게 여러 번 시도하며 상태를 알린다.
    """
    last = None
    for i in range(1, attempts + 1):
        print(f"  suno.com 접속 중... ({i}/{attempts})")
        try:
            page.goto(SUNO_URL, wait_until="commit", timeout=20_000)
            return True
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    print(f"suno.com 에 접속하지 못했습니다: {last}")
    return False


def _is_logged_in(page) -> bool:
    try:
        page.wait_for_function("() => typeof window.Clerk !== 'undefined'", timeout=30_000)
        page.wait_for_function(
            "() => window.Clerk.loaded === true || window.Clerk.session !== undefined",
            timeout=30_000,
        )
        return bool(page.evaluate("() => !!(window.Clerk && window.Clerk.session)"))
    except Exception:  # noqa: BLE001
        return False


def cmd_login(channel: str | None = None) -> int:
    """브라우저를 띄우고 사용자가 직접 로그인하도록 기다린다."""
    channel = _channel(channel)
    print(f"{BROWSERS[channel]} 로 진행합니다.")
    pw, ctx, page = _browser(headless=False, channel=channel)
    try:
        if not _goto_suno(page):
            return 1
        if _is_logged_in(page):
            print("이미 로그인돼 있습니다.")
            _remember_browser(channel)
            return 0

        print("\n브라우저 창에서 Suno 에 로그인해 주세요.")
        print("로그인이 끝나면 이 창이 자동으로 감지합니다. (최대 5분 대기)\n")
        for _ in range(150):  # 5분
            if _is_logged_in(page):
                print("로그인 확인됐습니다. 세션이 저장되어 다음부터는 자동입니다.")
                _remember_browser(channel)
                time.sleep(1.5)  # 쿠키 flush 여유
                return 0
            time.sleep(2)
        print("시간이 초과됐습니다. 다시 실행해 주세요.")
        return 1
    finally:
        _shutdown(pw, ctx)


def cmd_sync(headless: bool = False, channel: str | None = None) -> int:
    """Suno 에서 Liked 목록을 가져와 library.json 에 저장한다."""
    channel = _channel(channel)
    pw, ctx, page = _browser(headless=headless, channel=channel)
    try:
        if not _goto_suno(page):
            return 1
        if not _is_logged_in(page):
            print(f"{BROWSERS[channel]} 에서 로그인이 필요합니다.  login  을 먼저 실행하세요.")
            return NEED_LOGIN

        print("목록을 가져오는 중입니다... (곡이 많으면 1~2분 걸립니다)")
        result = page.evaluate(COLLECT_JS)
    finally:
        _shutdown(pw, ctx)

    songs = result.get("songs") or []
    if not songs:
        print("Liked 곡을 찾지 못했습니다.")
        return 1

    # 파일 번호를 곡 id 에 고정한다 — 새 곡이 추가돼도 기존 번호가 밀리지 않는다
    old = _load_library()
    index_map: dict[str, int] = {s["id"]: s["index"] for s in old.get("songs", []) if "index" in s}
    nxt = max(index_map.values(), default=0) + 1
    for s in songs:
        if s["id"] not in index_map:
            index_map[s["id"]] = nxt
            nxt += 1
        s["index"] = index_map[s["id"]]

    payload = {
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scanned_total": result.get("scanned_total"),
        "server_side_filter": result.get("server_side_filter"),
        "songs": songs,
    }
    LIBRARY.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Liked {len(songs)}곡을 {LIBRARY.name} 에 저장했습니다.")

    # Suno 에서 제목을 고쳤다면 받아둔 파일 이름도 따라가게 한다
    _report_renames(sync_filenames(songs))
    return 0


def sync_filenames(songs: list[dict], apply: bool = True) -> list[tuple[str, str, str]]:
    """Suno 에서 제목을 바꾼 곡의 로컬 파일 이름을 맞춰 준다.

    곡의 고유키(clip id)로 대조하므로 제목이 아무리 바뀌어도 짝이 흐트러지지 않는다.
    (fmt, 이전이름, 새이름) 목록을 돌려준다.
    """
    changes: list[tuple[str, str, str]] = []
    for fmt in ("wav", "mp3"):
        out = HERE / fmt
        if not out.is_dir():
            continue
        manifest = dl.Manifest(out / dl.MANIFEST_NAME)
        touched = False

        for song in songs:
            rec = manifest.entries.get(song["id"])
            if not rec:
                continue
            old = out / rec["file"]
            new = out / dl.safe_name(song["title"], song["index"], fmt)
            if old.name == new.name or not old.is_file():
                continue
            if new.exists():
                print(f"  건너뜀 — 같은 이름이 이미 있음: {new.name}")
                continue
            if apply:
                try:
                    old.rename(new)
                except OSError as e:
                    print(f"  이름 변경 실패: {old.name} ({e})")
                    continue
                rec["file"] = new.name
                rec["title"] = song["title"]
                touched = True
            changes.append((fmt, old.name, new.name))

        if touched:
            manifest.save()
    return changes


def _report_renames(changes: list[tuple[str, str, str]], apply: bool = True) -> None:
    if not changes:
        print("제목이 바뀐 곡은 없습니다.")
        return
    head = "이름을 바꿨습니다" if apply else "바뀔 예정입니다(--dry-run)"
    print(f"\n{head} — {len(changes)}개")
    for fmt, old, new in changes:
        print(f"  [{fmt}] {old}\n       -> {new}")


def _load_library() -> dict:
    if LIBRARY.is_file():
        try:
            return json.loads(LIBRARY.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


# --------------------------------------------------------------------------- #
# 다운로드 작업 (UI / CLI 공용)
# --------------------------------------------------------------------------- #
STATE: dict = {
    "running": False,
    "total": 0, "done": 0, "ok": 0, "skip": 0, "fail": 0,
    "current": "", "log": [], "finished": False,
}
_state_lock = threading.Lock()


def _push(msg: str) -> None:
    with _state_lock:
        STATE["log"].append(msg)
        del STATE["log"][:-300]  # 최근 300줄만 유지
    print(msg, flush=True)


class _WavMaker:
    """WAV 가 없는 곡을 만나면 그 자리에서 변환을 요청하고 기다린다.

    예전에는 목록 전체를 훑어 실패로 찍은 다음, 맨 마지막에 몰아서 변환을
    요청했다. 곡이 수백 개면 첫 훑기만 십수 분이라 화면에는 X 만 잔뜩 쌓이고
    다운로드는 시작조차 안 한 것처럼 보였다. 이제는 곡 하나마다
    요청 -> 대기 -> 받기 를 끝내므로 진행이 눈에 보인다.

    브라우저는 실제로 변환이 필요한 첫 곡에서만 연다. 전부 이미 있으면
    창이 뜨지 않는다.
    """

    def __init__(self, log=None) -> None:
        self.log = log or _push
        self._session: tuple | None = None
        self._off = False

    def _ready(self) -> bool:
        if self._off:
            return False
        if self._session:
            return True
        channel = _channel()
        self.log("")
        self.log(f"WAV 변환용 {BROWSERS[channel]} 창을 엽니다.")
        self.log("(Suno 에서 다운로드 버튼을 누르는 것과 같은 동작입니다)")
        try:
            pw, ctx, page = _browser(headless=False, channel=channel)
        except SystemExit as e:  # noqa: BLE001
            self.log(f"브라우저를 띄우지 못해 WAV 생성을 건너뜁니다: {e}")
            self._off = True
            return False
        if not _goto_suno(page) or not _is_logged_in(page):
            self.log(f"{BROWSERS[channel]} 에서 로그인이 필요합니다. WAV 생성을 건너뜁니다.")
            _shutdown(pw, ctx)
            self._off = True
            return False
        self._session = (pw, ctx, page)
        self.log("")
        return True

    def make(self, song: dict, wait: int = 90) -> int | None:
        """변환을 요청하고 CDN 에 올라오면 크기를, 못 만들면 None 을 반환."""
        if not self._ready():
            return None
        page = self._session[2]
        try:
            res = page.evaluate(CONVERT_JS, [song["id"]])
        except Exception as e:  # noqa: BLE001
            self.log(f"    변환 요청 실패: {e}")
            return None

        status = res[0]["status"] if res else "EX"
        if status not in (200, 201, 202, 204):
            self.log(f"    변환 요청이 거절됐습니다 (status {status})")
            return None

        deadline = time.time() + wait
        while time.time() < deadline:
            size = dl.remote_size(song["wav_url"])
            if size is not None:
                return size
            time.sleep(3)
        self.log("    시간 안에 생성되지 않았습니다. 다음 곡으로 넘어갑니다.")
        return None

    def close(self) -> None:
        if self._session:
            _shutdown(self._session[0], self._session[1])
            self._session = None


def run_download(songs: list[dict], formats: list[str], workers: int = 1,
                 make_wav: bool = False) -> None:
    with _state_lock:
        STATE.update(running=True, finished=False, total=len(songs) * len(formats),
                     done=0, ok=0, skip=0, fail=0, current="", log=[])
    maker = _WavMaker() if make_wav else None
    try:
        for fmt in formats:
            out = HERE / fmt
            out.mkdir(parents=True, exist_ok=True)
            manifest = dl.Manifest(out / dl.MANIFEST_NAME)
            _push(f"=== {fmt.upper()} — {len(songs)}곡 → {out} ===")

            for song in songs:
                dest = out / dl.safe_name(song["title"], song["index"], fmt)
                with _state_lock:
                    STATE["current"] = f"{fmt.upper()}  {dest.name}"

                if manifest.has(song["id"], out) or (dest.is_file() and dest.stat().st_size > 0):
                    manifest.add(song["id"], song["title"], dest)
                    status, msg = "skip", "이미 있음"
                else:
                    size = dl.remote_size(song[f"{fmt}_url"])
                    if size is None and fmt == "wav" and maker:
                        # 서버에 WAV 가 없다 — 지금 만들어 달라고 요청한다
                        _push(f"    WAV 생성 요청 — {song['title'][:46]}")
                        size = maker.make(song)
                    if size is None:
                        status, msg = "fail", f"{fmt.upper()} 없음 (서버 미생성)"
                    else:
                        status, msg = dl.download(song[f"{fmt}_url"], dest, size)
                        if status in ("ok", "skip"):
                            manifest.add(song["id"], song["title"], dest)

                with _state_lock:
                    STATE["done"] += 1
                    STATE[status] += 1
                    n, t = STATE["done"], STATE["total"]
                icon = {"ok": "✓", "skip": "-", "fail": "✗"}[status]
                _push(f"[{n}/{t}] {icon} {dest.name}  ({msg})")

                if STATE["done"] % 20 == 0:
                    manifest.save()
            manifest.save()

        _push(f"\n완료 — 받음 {STATE['ok']}, 건너뜀 {STATE['skip']}, 실패 {STATE['fail']}")
    except Exception as e:  # noqa: BLE001
        _push(f"오류로 중단됐습니다: {e}")
    finally:
        if maker:
            maker.close()
        with _state_lock:
            STATE["running"] = False
            STATE["finished"] = True
            STATE["current"] = ""


def _downloaded_ids(songs: list[dict] | None = None) -> dict[str, list[str]]:
    """포맷별로 이미 받아둔 clip id 목록.

    기록(downloaded.json)을 먼저 보고, 기록에 없더라도 예상 파일명이 실제로
    폴더에 있으면 받은 것으로 친다. 기록 파일을 지웠어도 상태가 맞게 나온다.
    """
    res: dict[str, list[str]] = {}
    for fmt in ("wav", "mp3"):
        out = HERE / fmt
        ids: set[str] = set()
        m = out / dl.MANIFEST_NAME
        if m.is_file():
            try:
                ids |= set(json.loads(m.read_text(encoding="utf-8")).get("entries", {}))
            except Exception:  # noqa: BLE001
                pass
        for s in songs or []:
            if s["id"] in ids:
                continue
            p = out / dl.safe_name(s["title"], s["index"], fmt)
            if p.is_file() and p.stat().st_size > 0:
                ids.add(s["id"])
        res[fmt] = sorted(ids)
    return res


# --------------------------------------------------------------------------- #
# 로컬 웹 UI
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 요청 로그 억제
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _serve_audio(self, clip_id: str) -> None:
        """미리듣기. 받아둔 MP3 가 있으면 로컬에서, 없으면 CDN 으로 넘긴다."""
        song = next((s for s in _load_library().get("songs", []) if s["id"] == clip_id), None)
        if not song:
            self._send(404, b"unknown id", "text/plain")
            return

        path = HERE / "mp3" / dl.safe_name(song["title"], song["index"], "mp3")
        if not (path.is_file() and path.stat().st_size > 0):
            self.send_response(302)  # 아직 안 받은 곡은 스트리밍으로
            self.send_header("Location", song["mp3_url"])
            self.end_headers()
            return

        # 탐색(seek)이 되려면 Range 요청을 처리해야 한다
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            try:
                start = int(first) if first else 0
                end = int(last) if last else size - 1
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 사용자가 곡을 바꾸거나 탐색하면 흔히 발생한다

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            if not UI_FILE.is_file():
                self._send(500, b"ui.html not found", "text/plain")
                return
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/songs":
            lib = _load_library()
            songs = lib.get("songs", [])
            self._json({
                "synced_at": lib.get("synced_at"),
                "songs": songs,
                "downloaded": _downloaded_ids(songs),
            })
        elif path == "/api/audio":
            ids = parse_qs(urlparse(self.path).query).get("id") or []
            if not ids:
                self._send(400, b"id required", "text/plain")
            else:
                self._serve_audio(ids[0])
        elif path == "/api/progress":
            with _state_lock:
                self._json({k: v for k, v in STATE.items()})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            body = {}

        if path == "/api/download":
            if STATE["running"]:
                self._json({"error": "이미 다운로드가 진행 중입니다."}, 409)
                return
            ids = set(body.get("ids") or [])
            formats = [f for f in (body.get("formats") or []) if f in ("wav", "mp3")]
            if not ids or not formats:
                self._json({"error": "곡과 포맷을 선택하세요."}, 400)
                return
            songs = [s for s in _load_library().get("songs", []) if s["id"] in ids]
            make_wav = bool(body.get("makewav"))
            threading.Thread(target=run_download, args=(songs, formats, 1, make_wav),
                             daemon=True).start()
            self._json({"started": len(songs), "formats": formats, "makewav": make_wav})
        else:
            self._json({"error": "not found"}, 404)


def cmd_ui(port: int = 8777, open_browser: bool = True) -> int:
    if not _load_library().get("songs"):
        print("목록이 없습니다.  python suno.py sync  를 먼저 실행하세요.")
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n웹 UI: {url}")
    print("종료하려면 이 창에서 Ctrl+C 를 누르세요.\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        srv.server_close()
    return 0


# --------------------------------------------------------------------------- #
# WAV 생성 요청
# --------------------------------------------------------------------------- #
# Suno 는 곡을 만들 때 WAV 를 미리 만들어 두지 않는다. 누군가 한 번 다운로드를
# 눌러야 그때 변환된다. 아래 엔드포인트가 그 "한 번 누르기"에 해당한다.
CONVERT_JS = r"""
async (ids) => {
  const API = 'https://studio-api.prod.suno.com';
  const out = [];
  for (const id of ids) {
    try {
      const token = await window.Clerk.session.getToken();
      const r = await fetch(API + '/api/gen/' + id + '/convert_wav/', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        credentials: 'include',
        body: '{}',
      });
      out.push({ id, status: r.status });
    } catch (e) {
      out.push({ id, status: 'EX' });
    }
    await new Promise(r => setTimeout(r, 600));
  }
  return out;
}
"""


def convert_wavs(need: list[dict], channel: str | None = None,
                 log=print) -> int:
    """WAV 변환을 요청한다. 성공 요청 수를 돌려준다."""
    channel = _channel(channel)
    pw, ctx, page = _browser(headless=False, channel=channel)
    ok = 0
    try:
        if not _goto_suno(page) or not _is_logged_in(page):
            log(f"{BROWSERS[channel]} 에서 로그인이 필요합니다.")
            return 0
        for r in page.evaluate(CONVERT_JS, [s["id"] for s in need]):
            good = r["status"] in (200, 201, 202, 204)
            ok += good
            title = next(s["title"] for s in need if s["id"] == r["id"])
            log(f"  {'OK' if good else '실패(%s)' % r['status']:10} {title[:46]}")
    finally:
        _shutdown(pw, ctx)
    return ok


def wait_for_wavs(need: list[dict], log=print, rounds: int = 20) -> list[dict]:
    """생성될 때까지 기다린다. 준비된 곡 목록을 돌려준다."""
    ready, pending = [], list(need)
    for _ in range(rounds):
        time.sleep(6)
        still = []
        for s in pending:
            if dl.remote_size(s["wav_url"]) is not None:
                ready.append(s)
                log(f"  준비됨 — {s['title'][:46]}")
            else:
                still.append(s)
        pending = still
        if not pending:
            break
    for s in pending:
        log(f"  아직 생성 안 됨 — {s['title'][:46]}")
    return ready


def cmd_makewav(dry_run: bool = False, channel: str | None = None,
                download: bool = True) -> int:
    """서버에 WAV 가 없는 곡의 변환을 요청하고, 생성되면 받아 둔다."""
    songs = _load_library().get("songs", [])
    if not songs:
        print("목록이 없습니다.  sync  를 먼저 실행하세요.")
        return 1

    out = HERE / "wav"
    out.mkdir(parents=True, exist_ok=True)
    manifest = dl.Manifest(out / dl.MANIFEST_NAME)
    titled = [s for s in songs if s["title"].strip().lower() != "untitled"]
    local_missing = [
        s for s in titled
        if not manifest.has(s["id"], out)
        and not (out / dl.safe_name(s["title"], s["index"], "wav")).is_file()
    ]
    if not local_missing:
        print("모든 곡의 WAV 를 이미 갖고 있습니다.")
        return 0

    print(f"로컬에 WAV 가 없는 곡 {len(local_missing)}곡 — 서버 상태 확인 중...")
    need, ready = [], []
    for s in local_missing:
        (ready if dl.remote_size(s["wav_url"]) is not None else need).append(s)
        time.sleep(0.5)  # 몰아서 요청하면 차단당해 오탐이 난다

    if ready:
        print(f"  이미 서버에 있음 (바로 받을 수 있음): {len(ready)}곡")
    if not need:
        print("변환이 필요한 곡은 없습니다.")
    else:
        print(f"  변환이 필요함: {len(need)}곡")
        for s in need:
            print(f"     #{s['index']:<4} {s['title'][:46]}")

    if dry_run:
        print("\n--dry-run 이라 여기서 멈춥니다.")
        return 0

    if need:
        print("\n※ 변환 요청은 Suno 에서 다운로드 버튼을 누르는 것과 같은 동작입니다.")
        print("   2026-09-03 부터는 월 다운로드 한도에 포함될 수 있습니다.\n")
        print(f"{len(need)}곡 변환 요청 중...")
        convert_wavs(need, channel)
        print("생성될 때까지 기다리는 중...")
        ready += wait_for_wavs(need)

    if ready and download:
        print(f"\nWAV {len(ready)}곡 내려받는 중...")
        run_download(ready, ["wav"])
    return 0


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Suno 라이브러리 다운로더")
    sub = ap.add_subparsers(dest="cmd")

    p_login = sub.add_parser("login", help="브라우저를 띄워 로그인 (최초 1회)")
    p_login.add_argument("--browser", choices=("chrome", "msedge"),
                         help="쓸 브라우저 (기본: 저장된 설정, 없으면 chrome)")

    p_sync = sub.add_parser("sync", help="Liked 목록 새로 가져오기")
    p_sync.add_argument("--headless", action="store_true", help="브라우저 창 없이 실행")
    p_sync.add_argument("--browser", choices=("chrome", "msedge"))

    p_ui = sub.add_parser("ui", help="웹 UI 열기")
    p_ui.add_argument("--port", type=int, default=8777)

    p_mw = sub.add_parser("makewav", help="서버에 WAV 가 없는 곡의 변환을 요청하고 받아 둔다")
    p_mw.add_argument("--dry-run", action="store_true", help="요청하지 않고 대상만 확인")
    p_mw.add_argument("--browser", choices=("chrome", "msedge"))
    p_mw.add_argument("--no-download", action="store_true", help="변환만 하고 받지 않기")

    p_ren = sub.add_parser("rename", help="제목이 바뀐 곡의 파일 이름 맞추기 (sync 시 자동 실행됨)")
    p_ren.add_argument("--dry-run", action="store_true", help="바꾸지 않고 목록만 보기")

    p_get = sub.add_parser("get", help="UI 없이 다운로드")
    p_get.add_argument("--all", action="store_true", help="전체 받기")
    p_get.add_argument("--search", help="제목에 이 문자열이 들어간 곡만")
    p_get.add_argument("--format", default="wav", help="wav / mp3 / wav,mp3")
    p_get.add_argument("--skip-untitled", action="store_true")

    args = ap.parse_args()

    if args.cmd == "login":
        return cmd_login(args.browser)
    if args.cmd == "sync":
        return cmd_sync(headless=args.headless, channel=args.browser)
    if args.cmd == "ui":
        return cmd_ui(port=args.port)
    if args.cmd == "makewav":
        return cmd_makewav(dry_run=args.dry_run, channel=args.browser,
                           download=not args.no_download)
    if args.cmd == "rename":
        songs = _load_library().get("songs", [])
        if not songs:
            print("목록이 없습니다.  sync  를 먼저 실행하세요.")
            return 1
        _report_renames(sync_filenames(songs, apply=not args.dry_run), apply=not args.dry_run)
        return 0
    if args.cmd == "get":
        songs = _load_library().get("songs", [])
        if not songs:
            print("목록이 없습니다.  python suno.py sync  를 먼저 실행하세요.")
            return 1
        if args.search:
            songs = [s for s in songs if args.search.lower() in s["title"].lower()]
        if args.skip_untitled:
            songs = [s for s in songs if s["title"].strip().lower() != "untitled"]
        if not args.all and not args.search:
            print("--all 또는 --search 를 지정하세요.")
            return 1
        formats = [f.strip() for f in args.format.split(",") if f.strip() in ("wav", "mp3")]
        if not formats:
            print("--format 은 wav / mp3 / wav,mp3 중에서 지정하세요.")
            return 1
        if not songs:
            print("조건에 맞는 곡이 없습니다.")
            return 1
        print(f"{len(songs)}곡 × {formats} 다운로드")
        run_download(songs, formats)
        return 0 if STATE["fail"] == 0 else 2

    # 인자 없이 실행 — 로그인 확인 → 동기화 → UI
    channel = _channel()
    if not _profile_dir(channel).exists():
        print(f"최초 실행입니다. {BROWSERS[channel]} 로그인 창을 띄웁니다.")
        print()
        if cmd_login(channel) != 0:
            return 1

    rc = cmd_sync(channel=channel)
    if rc == NEED_LOGIN:
        # 프로필은 있는데 세션이 만료(또는 유실)된 경우 — 로그인부터 다시.
        print()
        if cmd_login(channel) != 0:
            return 1
        rc = cmd_sync(channel=channel)
    if rc != 0:
        return 1
    return cmd_ui()


if __name__ == "__main__":
    _init_console()
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"\n예기치 못한 오류: {e}")
        code = 1
    # exe 를 더블클릭한 경우 오류 메시지를 볼 새도 없이 창이 닫히는 것을 막는다
    if getattr(sys, "frozen", False) and code not in (0, 130):
        try:
            input("\n계속하려면 Enter 를 누르세요...")
        except EOFError:
            pass
    sys.exit(code)
