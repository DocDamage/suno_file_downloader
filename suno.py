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
import re
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

  // audio_url 은 요즘 '/api/forbidden' 으로 막혀서 내려온다. 실제 주소는
  // media_urls 안에 있으므로 거기서 골라 쓴다.
  const usable = (u) => (typeof u === 'string' && u && u.indexOf('/api/forbidden') === -1)
    ? u : null;
  const pick = (c, kind) => {
    const media = Array.isArray(c.media_urls) ? c.media_urls : [];
    const hit = media.find(m => m && typeof m.content_type === 'string'
                                && m.content_type.indexOf(kind) === 0 && usable(m.url));
    return hit ? hit.url : null;
  };

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
      mp3_url: pick(c, 'mp3') || usable(c.audio_url)
               || ('https://cdn1.suno.ai/' + c.id + '.mp3'),
      m4a_url: pick(c, 'm4a'),
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


def _settle(page, timeout: float = 15.0) -> None:
    """SPA 가 스스로 한 번 더 이동하는 것까지 가라앉기를 기다린다."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=int(timeout * 1000))
    except Exception:  # noqa: BLE001
        pass
    try:  # Clerk 이 뜨면 앱 초기화가 끝난 것으로 본다
        page.wait_for_function(
            "() => window.Clerk && window.Clerk.loaded === true", timeout=int(timeout * 1000)
        )
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.0)


def _evaluate(page, js: str, arg=None, attempts: int = 4):
    """page.evaluate 를 재시도와 함께 실행한다.

    suno.com 은 첫 로딩 직후 내부적으로 한 번 더 이동한다. 그 순간에 스크립트가
    돌고 있으면 'Execution context was destroyed' 로 통째로 죽는다. 페이지가
    가라앉기를 기다렸다가 실행하고, 그래도 걸리면 다시 시도한다.
    """
    last = None
    for i in range(1, attempts + 1):
        _settle(page)
        try:
            return page.evaluate(js, arg) if arg is not None else page.evaluate(js)
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            transient = (
                "Execution context was destroyed" in msg
                or "navigating" in msg
                or "Target closed" in msg
                or "Cannot find context" in msg
            )
            if not transient or i == attempts:
                raise
            print(f"  페이지가 이동해 다시 시도합니다 ({i}/{attempts})...")
            time.sleep(2)
    raise last  # 여기까지 오지 않는다


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
        result = _evaluate(page, COLLECT_JS)
    finally:
        _shutdown(pw, ctx)

    songs = result.get("songs") or []
    if not songs:
        print("Liked 곡을 찾지 못했습니다.")
        return 1

    # 번호는 만든 날짜 순서로 매긴다 — 가장 오래된 곡이 1번.
    # 순위로 정의하면 어느 PC 에서 계산해도 같은 번호가 나온다.
    for rank, s2 in enumerate(
            sorted(songs, key=lambda x: (x.get("created_at") or "", x["id"])), start=1):
        s2["index"] = rank

    payload = {
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scanned_total": result.get("scanned_total"),
        "server_side_filter": result.get("server_side_filter"),
        "songs": songs,
    }
    LIBRARY.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Liked {len(songs)}곡을 {LIBRARY.name} 에 저장했습니다.")

    # 목록에서 사라진 곡의 파일을 먼저 치운다. 그래야 번호가 비어
    # 다시 매겨진 곡과 이름이 부딪히지 않는다.
    cleanup_removed(songs, quiet=True)
    # Suno 에서 제목을 고쳤다면 받아둔 파일 이름도 따라가게 한다
    _report_renames(sync_filenames(songs))
    return 0


def sync_filenames(songs: list[dict], apply: bool = True) -> list[tuple[str, str, str]]:
    """파일 이름을 현재 제목·번호에 맞춘다.

    곡의 고유키(clip id)로 기록과 대조하므로 제목이 바뀌든 번호가 다시
    매겨지든 짝이 흐트러지지 않는다.

    번호가 통째로 바뀌면 서로 자리를 맞바꾸는 곡이 생긴다(1번이 785번이 되고
    785번이 1번이 되는 식). 그대로 하나씩 옮기면 "이미 있는 이름" 이라 전부
    막히므로, 임시 이름으로 한 번 비켜 두었다가 최종 이름으로 옮긴다.

    (fmt, 이전이름, 새이름) 목록을 돌려준다.
    """
    changes: list[tuple[str, str, str]] = []
    for fmt in ("wav", "mp3"):
        out = HERE / fmt
        if not out.is_dir():
            continue
        manifest = dl.Manifest(out / dl.MANIFEST_NAME)

        plan: list[tuple[dict, Path, Path]] = []
        for song in songs:
            rec = manifest.entries.get(song["id"])
            if not rec:
                continue
            old = out / rec["file"]
            if not old.is_file():
                continue
            # 실제 파일의 확장자를 그대로 유지한다
            new = out / dl.safe_name(song["title"], song["index"], old.suffix.lstrip("."))
            if old.name != new.name:
                plan.append((song, old, new))

        if not plan:
            continue
        if not apply:
            changes += [(fmt, o.name, n.name) for _, o, n in plan]
            continue

        # 1단계 — 옮길 파일을 전부 임시 이름으로 비켜 둔다
        staged: list[tuple[dict, Path, Path, str]] = []
        for song, old, new in plan:
            tmp = out / f".renaming-{song['id']}{old.suffix}"
            try:
                old.rename(tmp)
            except OSError as e:
                print(f"  이름 변경 실패: {old.name} ({e})")
                continue
            staged.append((song, tmp, new, old.name))

        # 2단계 — 최종 이름으로
        for song, tmp, new, oldname in staged:
            if new.exists():
                print(f"  건너뜀 — 같은 이름이 이미 있음: {new.name}")
                try:
                    tmp.rename(out / oldname)      # 원래 이름으로 되돌린다
                except OSError:
                    print(f"  ! 되돌리지 못했습니다: {tmp.name}")
                continue
            try:
                tmp.rename(new)
            except OSError as e:
                print(f"  이름 변경 실패: {new.name} ({e})")
                try:
                    tmp.rename(out / oldname)
                except OSError:
                    print(f"  ! 되돌리지 못했습니다: {tmp.name}")
                continue
            manifest.add(song["id"], song["title"], new)
            changes.append((fmt, oldname, new.name))

        manifest.save()
    return changes


# 목록에서 사라진 곡이 이 비율을 넘으면 자동 정리를 멈춘다. 동기화가 잘못돼
# 목록이 반쪽만 왔을 때 파일을 무더기로 치우는 사고를 막는다.
CLEANUP_GUARD = 0.10


def cleanup_removed(songs: list[dict], apply: bool = True, purge: bool = False,
                    quiet: bool = False) -> int:
    """서버 목록에 없는 곡의 로컬 파일을 치운다.

    좋아요를 해제하거나 Suno 에서 지운 곡이 여기 해당한다. 그냥 두면 옛 번호를
    단 채 남아 새로 번호를 받은 곡과 이름이 부딪힌다.

    9/3 이후로는 다시 받기 어려우므로 기본은 삭제가 아니라 `_removed/` 로
    옮기기다. 정말 지우려면 purge=True.
    """
    live = {s["id"] for s in songs}
    if not live:
        return 0

    targets: list[tuple[str, Path, str, str]] = []   # (fmt, path, clip_id, title)
    total_known = 0
    for fmt in ("wav", "mp3"):
        out = HERE / fmt
        if not out.is_dir():
            continue
        manifest = dl.Manifest(out / dl.MANIFEST_NAME)
        total_known += len(manifest.entries)
        for cid, rec in manifest.entries.items():
            if cid in live:
                continue
            p = out / rec["file"]
            if p.is_file():
                targets.append((fmt, p, cid, rec.get("title", "")))

    if not targets:
        if not quiet:
            print("서버 목록에 없는 파일은 없습니다.")
        return 0

    # 안전장치 — 한꺼번에 너무 많이 사라졌다면 동기화가 잘못됐을 수 있다
    stale_ids = {cid for _, _, cid, _ in targets}
    if total_known and len(stale_ids) > max(20, total_known * CLEANUP_GUARD):
        print(f"\n⚠ 서버 목록에 없는 곡이 {len(stale_ids)}개나 됩니다. "
              f"동기화가 잘못됐을 수 있어 자동 정리를 건너뜁니다.")
        print("  확인 후 정리하려면:  cleanup --dry-run  으로 먼저 보세요.")
        return 0

    where = "지웁니다" if purge else f"{HERE / '_removed'} 로 옮깁니다"
    print(f"\n서버 목록에 없는 곡 {len(stale_ids)}개 · 파일 {len(targets)}개를 "
          f"{'정리할 예정입니다' if not apply else where}.")
    for fmt, p, _, _ in targets:
        print(f"  [{fmt}] {p.name}  ({p.stat().st_size / 2**20:.1f} MB)")
    if not apply:
        print("\n--dry-run 이라 파일은 그대로입니다.")
        return 0

    moved = 0
    for fmt in ("wav", "mp3"):
        out = HERE / fmt
        if not out.is_dir():
            continue
        manifest = dl.Manifest(out / dl.MANIFEST_NAME)
        touched = False
        for f, p, cid, _ in [t for t in targets if t[0] == fmt]:
            try:
                if purge:
                    p.unlink()
                else:
                    trash = HERE / "_removed" / fmt
                    trash.mkdir(parents=True, exist_ok=True)
                    dest = trash / p.name
                    n = 1
                    while dest.exists():           # 같은 이름이 이미 있으면 번호를 붙인다
                        dest = trash / f"{p.stem} ({n}){p.suffix}"
                        n += 1
                    p.rename(dest)
                moved += 1
            except OSError as e:
                print(f"  실패: {p.name} ({e})")
                continue
            manifest.entries.pop(cid, None)
            touched = True
        if touched:
            manifest.save()

    print(f"\n{'삭제' if purge else '이동'} 완료 — {moved}개")
    if not purge:
        print(f"  필요 없으면 {HERE / '_removed'} 폴더를 지우세요.")
    return moved


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
# 직접 넣은 WAV 받아들이기
# --------------------------------------------------------------------------- #
# Suno 사이트에서 손으로 받은 WAV 를 wav/ 폴더에 넣어두면, 그 파일이 어느 곡인지
# 찾아내 기록에 넣고 "번호 - 곡이름.wav" 로 정리한 뒤 MP3 까지 만든다.
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_DUP_SUFFIX = re.compile(r"\s*[(\[]\s*\d+\s*[)\]]\s*$")      # "제목 (1)" 같은 꼬리표
_KEEP = re.compile(r"[^0-9a-z\uac00-\ud7a3\u3131-\u318e\u3040-\u30ff\u4e00-\u9fff]+")


def _norm_title(t: str) -> str:
    """제목 비교용으로 다듬는다. 문장부호·공백·대소문자 차이를 없앤다."""
    t = _DUP_SUFFIX.sub("", t.strip())
    return _KEEP.sub("", t.lower())


def _wav_seconds(path: Path) -> float | None:
    """WAV 재생 길이(초). 표준 라이브러리만 쓴다."""
    try:
        import wave

        with wave.open(str(path)) as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate) if rate else None
    except Exception:  # noqa: BLE001
        return None


def _match_song(path: Path, songs: list[dict], by_index: dict, by_title: dict) -> tuple:
    """파일에 맞는 곡을 찾는다. (곡, 근거) 또는 (None, 사유)."""
    stem = path.stem

    # 1) 파일명에 clip id 가 들어 있으면 확실하다
    hit = _UUID_RE.search(stem)
    if hit:
        for s in songs:
            if s["id"].lower() == hit.group(0).lower():
                return s, "파일명의 곡 ID"

    # 2) 우리가 붙이는 "NNN - " 번호
    m = re.match(r"^(\d{3})\s*-\s*", stem)
    if m:
        s = by_index.get(int(m.group(1)))
        if s:
            return s, "파일명의 번호"

    # 3) 제목으로 찾는다
    cands = by_title.get(_norm_title(stem), [])
    if len(cands) == 1:
        return cands[0], "제목 일치"
    if len(cands) > 1:
        # 제목이 같은 곡이 여럿이면 재생 길이로 가른다
        secs = _wav_seconds(path)
        if secs is not None:
            near = [c for c in cands
                    if c.get("duration") and abs(c["duration"] - secs) <= 1.5]
            if len(near) == 1:
                return near[0], f"제목 + 길이({secs:.0f}초)"
            if len(near) > 1:
                return None, f"제목과 길이가 같은 곡이 {len(near)}개라 가릴 수 없음"
        return None, f"같은 제목의 곡이 {len(cands)}개라 가릴 수 없음"

    return None, "목록에서 같은 제목을 찾지 못함"


def scan_wavs(dry_run: bool = False, make_mp3: bool = True, quiet: bool = False) -> int:
    """wav/ 폴더의 기록에 없는 파일을 받아들인다. 처리한 개수를 반환."""
    songs = _load_library().get("songs", [])
    if not songs:
        if not quiet:
            print("목록이 없습니다.  sync  를 먼저 실행하세요.")
        return 0

    out = HERE / "wav"
    if not out.is_dir():
        return 0
    manifest = dl.Manifest(out / dl.MANIFEST_NAME)

    known = {rec["file"] for rec in manifest.entries.values()}
    orphans = [p for p in sorted(out.glob("*.wav"))
               if p.name not in known and p.stat().st_size > 0]
    if not orphans:
        if not quiet:
            print("wav 폴더에 새로 넣은 파일이 없습니다.")
        return 0

    print(f"\nwav 폴더에서 기록에 없는 파일 {len(orphans)}개를 찾았습니다.")

    by_index = {s["index"]: s for s in songs}
    by_title: dict[str, list[dict]] = {}
    for s in songs:
        by_title.setdefault(_norm_title(s["title"]), []).append(s)

    taken = {sid for sid in manifest.entries}          # 이미 WAV 가 있는 곡
    handled, skipped = [], []

    for path in orphans:
        song, why = _match_song(path, songs, by_index, by_title)
        if song is None:
            skipped.append((path.name, why))
            continue
        if song["id"] in taken:
            skipped.append((path.name, f"#{song['index']} 는 이미 WAV 가 있습니다"))
            continue

        dest = out / dl.safe_name(song["title"], song["index"], "wav")
        if dry_run:
            handled.append((path.name, dest.name, why, "—"))
            taken.add(song["id"])
            continue

        if dest.exists() and dest.resolve() != path.resolve():
            skipped.append((path.name, f"같은 이름이 이미 있음: {dest.name}"))
            continue
        try:
            if dest.name != path.name:
                path.rename(dest)
        except OSError as e:
            skipped.append((path.name, f"이름 변경 실패 ({e})"))
            continue

        manifest.add(song["id"], song["title"], dest)
        taken.add(song["id"])

        mp3_msg = "건너뜀"
        if make_mp3:
            mp3_out = HERE / "mp3"
            mp3_dest = mp3_out / dl.safe_name(song["title"], song["index"], "mp3")
            mp3_man = dl.Manifest(mp3_out / dl.MANIFEST_NAME)
            if mp3_man.has(song["id"], mp3_out) or mp3_dest.is_file():
                mp3_msg = "이미 있음"
            else:
                st, ms = dl.to_mp3(dest, mp3_dest)
                mp3_msg = ms if st == "ok" else f"실패: {ms}"
                if st == "ok":
                    mp3_man.add(song["id"], song["title"], mp3_dest)
                    mp3_man.save()
        handled.append((path.name, dest.name, why, mp3_msg))

    if not dry_run and handled:
        manifest.save()

    if handled:
        print(f"\n{'받아들일 예정' if dry_run else '정리했습니다'} — {len(handled)}개")
        for src, dst, why, mp3 in handled:
            print(f"  {src}")
            print(f"    -> {dst}   ({why})")
            if make_mp3 and not dry_run:
                print(f"       MP3: {mp3}")
    if skipped:
        print(f"\n손대지 않은 파일 — {len(skipped)}개")
        for name, why in skipped:
            print(f"  {name}\n    ({why})")
    return len(handled)


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


# 새로 만든 곡의 WAV 는 cdn1.suno.ai 에 올라오지 않는다. 변환을 요청한 뒤
# /api/gen/{id}/wav_file/ 가 알려주는 '서명된 S3 주소'로 받아야 한다.
# 그 주소는 GET 전용으로 서명돼 있어 HEAD 로 크기를 미리 잴 수 없다.
WAV_URL_JS = r"""
async (clipId) => {
  const API = 'https://studio-api.prod.suno.com';
  const tok = () => window.Clerk.session.getToken();

  const wavUrl = async () => {
    const r = await fetch(API + `/api/gen/${clipId}/wav_file/`, {
      headers: { Authorization: 'Bearer ' + (await tok()) }, credentials: 'include' });
    if (!r.ok) return null;
    let j = null;
    try { j = await r.json(); } catch (e) { return null; }
    return (j && j.wav_file_url) || null;
  };

  let url = await wavUrl();          // 이미 만들어져 있으면 바로 준다
  if (url) return { ok: true, url };

  const post = await fetch(API + `/api/gen/${clipId}/convert_wav/`, {
    method: 'POST', credentials: 'include',
    headers: { Authorization: 'Bearer ' + (await tok()), 'Content-Type': 'application/json' },
    body: '{}' });
  if (![200, 201, 202, 204].includes(post.status)) {
    return { ok: false, status: post.status };
  }

  for (let i = 0; i < 40; i++) {     // 최대 약 2분
    await new Promise(r => setTimeout(r, 3000));
    url = await wavUrl();
    if (url) return { ok: true, url };
  }
  return { ok: false, status: 'timeout' };
}
"""


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

    def make(self, song: dict) -> str | None:
        """변환을 요청하고 받을 수 있는 주소를 돌려준다. 실패하면 None."""
        if not self._ready():
            return None
        page = self._session[2]
        try:
            res = _evaluate(page, WAV_URL_JS, song["id"])
        except Exception as e:  # noqa: BLE001
            self.log(f"    변환 요청 실패: {e}")
            return None
        if res and res.get("ok"):
            return res["url"]
        self.log(f"    WAV 를 만들지 못했습니다 (status {res.get('status') if res else '?'})")
        return None

    def close(self) -> None:
        if self._session:
            _shutdown(self._session[0], self._session[1])
            self._session = None


def _local_file(man, out: Path, song: dict, ext: str) -> Path | None:
    """이미 갖고 있는 파일. 기록을 먼저 보고, 없으면 예상 이름으로 확인한다."""
    if man:
        p = man.has(song["id"], out)
        if p:
            return p
    p = out / dl.safe_name(song["title"], song["index"], ext)
    return p if p.is_file() and p.stat().st_size > 0 else None


def _fetch_wav(song: dict, dest: Path, maker) -> tuple[str, str]:
    """WAV 를 내려받는다. 서버에 없으면 만들어 달라고 요청한 뒤 받는다."""
    url = song["wav_url"]
    size = dl.remote_size(url)
    if size is None and maker:
        _push(f"    WAV 생성 요청 — {song['title'][:46]}")
        signed = maker.make(song)
        if signed:
            # 새로 만든 WAV 는 서명된 주소로 온다. GET 전용이라 크기를 못 잰다.
            url, size = signed, -1
    if size is None:
        return "fail", "WAV 없음 (서버 미생성)"
    return dl.download(url, dest, None if size < 0 else size)


def run_download(songs: list[dict], formats: list[str], workers: int = 1,
                 make_wav: bool = False) -> None:
    """WAV 는 서버에서 받고, MP3 는 그 WAV 에서 변환해 만든다.

    Suno 가 MP3 직접 내려받기를 막았기 때문에 MP3 를 따로 받지 않는다.
    무손실 WAV 한 번만 받아 두면 MP3 는 언제든 다시 만들 수 있고,
    변환은 Suno 를 거치지 않으므로 다운로드 한도와도 무관하다.
    """
    want = [f for f in ("wav", "mp3") if f in formats]
    with _state_lock:
        STATE.update(running=True, finished=False, total=len(songs) * len(want),
                     done=0, ok=0, skip=0, fail=0, current="", log=[])

    maker = _WavMaker() if make_wav else None
    wav_out, mp3_out = HERE / "wav", HERE / "mp3"
    wav_out.mkdir(parents=True, exist_ok=True)
    wav_man = dl.Manifest(wav_out / dl.MANIFEST_NAME)
    mp3_man = None
    if "mp3" in want:
        mp3_out.mkdir(parents=True, exist_ok=True)
        mp3_man = dl.Manifest(mp3_out / dl.MANIFEST_NAME)

    def tick(status: str, name: str, msg: str) -> None:
        with _state_lock:
            STATE["done"] += 1
            STATE[status] += 1
            n, t = STATE["done"], STATE["total"]
        icon = {"ok": "✓", "skip": "-", "fail": "✗"}[status]
        _push(f"[{n}/{t}] {icon} {name}  ({msg})")

    try:
        _push(f"=== {len(songs)}곡 · {' + '.join(f.upper() for f in want)} ===")
        if "mp3" in want and not dl.ffmpeg_exe():
            _push("⚠ ffmpeg 이 없어 MP3 를 만들 수 없습니다.  pip install imageio-ffmpeg")

        for song in songs:
            wav_path = _local_file(wav_man, wav_out, song, "wav")

            # ---- WAV ----
            if "wav" in want:
                with _state_lock:
                    STATE["current"] = f"WAV  {song['title'][:40]}"
                if wav_path:
                    wav_man.add(song["id"], song["title"], wav_path)
                    tick("skip", wav_path.name, "이미 있음")
                else:
                    dest = wav_out / dl.safe_name(song["title"], song["index"], "wav")
                    status, msg = _fetch_wav(song, dest, maker)
                    if status in ("ok", "skip"):
                        wav_man.add(song["id"], song["title"], dest)
                        wav_path = dest
                    tick(status, dest.name, msg)

            # ---- MP3 (WAV 에서 변환) ----
            if "mp3" in want:
                have = _local_file(mp3_man, mp3_out, song, "mp3")
                dest = mp3_out / dl.safe_name(song["title"], song["index"], "mp3")
                if have:
                    mp3_man.add(song["id"], song["title"], have)
                    tick("skip", have.name, "이미 있음")
                else:
                    if wav_path is None:
                        # 변환하려면 원본이 필요하다. WAV 를 요청하지 않았어도 받는다.
                        src = wav_out / dl.safe_name(song["title"], song["index"], "wav")
                        with _state_lock:
                            STATE["current"] = f"WAV(변환용)  {song['title'][:34]}"
                        st, _ = _fetch_wav(song, src, maker)
                        if st in ("ok", "skip"):
                            wav_man.add(song["id"], song["title"], src)
                            wav_path = src
                    if wav_path is None:
                        tick("fail", dest.name, "원본 WAV 가 없어 변환할 수 없습니다")
                    else:
                        with _state_lock:
                            STATE["current"] = f"MP3 변환  {song['title'][:34]}"
                        status, msg = dl.to_mp3(wav_path, dest)
                        if status == "ok":
                            mp3_man.add(song["id"], song["title"], dest)
                        tick(status, dest.name, msg)

            if STATE["done"] % 20 == 0:
                wav_man.save()
                if mp3_man:
                    mp3_man.save()

        wav_man.save()
        if mp3_man:
            mp3_man.save()
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

        # 받아둔 파일로 재생한다. MP3 가 없으면 WAV 로 대신한다 —
        # Suno 의 스트리밍 주소(cdn1 mp3)는 막혔고 m4a 는 암호화돼 있어 못 쓴다.
        path = None
        for folder, ext in (("mp3", "mp3"), ("wav", "wav")):
            out = HERE / folder
            rec = dl.Manifest(out / dl.MANIFEST_NAME).entries.get(clip_id)
            cands = [out / rec["file"]] if rec else []
            cands.append(out / dl.safe_name(song["title"], song["index"], ext))
            for c in cands:
                if c.is_file() and c.stat().st_size > 0:
                    path = c
                    break
            if path:
                break

        if path is None:
            self._send(404, "재생할 파일이 없습니다. 먼저 내려받으세요.".encode("utf-8"),
                       "text/plain; charset=utf-8")
            return

        ctype = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"

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
        self.send_header("Content-Type", ctype)
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


def _ui_running(port: int) -> bool:
    """그 포트에 이미 이 프로그램의 UI 서버가 떠 있는지 확인한다.

    /api/progress 는 이 프로그램에만 있는 주소라, 다른 프로그램이 같은 포트를
    쓰고 있는 경우와 구분된다.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/progress", timeout=2
        ) as r:
            if r.status != 200:
                return False
            return "running" in json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001
        return False


def cmd_ui(port: int = 8777, open_browser: bool = True) -> int:
    if not _load_library().get("songs"):
        print("목록이 없습니다.  python suno.py sync  를 먼저 실행하세요.")
        return 1
    url = f"http://127.0.0.1:{port}/"

    # 이미 떠 있는지 bind 하기 "전에" 물어본다.
    # Windows 는 HTTPServer 의 allow_reuse_address(SO_REUSEADDR) 때문에 이미
    # 쓰이는 포트에도 bind 가 그냥 성공한다. 그래서 bind 실패로는 알 수 없고,
    # 그대로 두면 서버가 두 개 떠서 요청이 어디로 갈지 알 수 없게 된다.
    # 바탕화면 바로가기를 여러 번 눌러도 UI 가 열리게 하려는 것이기도 하다.
    if _ui_running(port):
        print(f"이미 실행 중입니다 — {url}")
        if open_browser:
            webbrowser.open(url)
        return 0

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:  # noqa: BLE001
        print(f"{port} 포트를 열지 못했습니다: {e}")
        print("  --port 로 다른 번호를 지정하세요.  예:  run.bat ui --port 8778")
        return 1
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
def cmd_makewav(dry_run: bool = False, channel: str | None = None,
                download: bool = True) -> int:
    """WAV 가 없는 곡을 찾아 변환을 요청하고 받아 둔다."""
    songs = _load_library().get("songs", [])
    if not songs:
        print("목록이 없습니다.  sync  를 먼저 실행하세요.")
        return 1

    out = HERE / "wav"
    out.mkdir(parents=True, exist_ok=True)
    manifest = dl.Manifest(out / dl.MANIFEST_NAME)
    need = [
        s for s in songs
        if s["title"].strip().lower() != "untitled"
        and not manifest.has(s["id"], out)
        and not (out / dl.safe_name(s["title"], s["index"], "wav")).is_file()
    ]
    if not need:
        print("모든 곡의 WAV 를 이미 갖고 있습니다.")
        return 0

    print(f"WAV 가 없는 곡 {len(need)}곡")
    for s in need:
        print(f"   #{s['index']:<4} {s['title'][:46]}")
    if dry_run:
        print("\n--dry-run 이라 여기서 멈춥니다.")
        return 0
    if not download:
        return 0

    print("\n※ 변환 요청은 Suno 에서 다운로드 버튼을 누르는 것과 같은 동작입니다.")
    print("   2026-09-03 부터는 월 다운로드 한도에 포함될 수 있습니다.\n")
    run_download(need, ["wav"], make_wav=True)
    return 0 if STATE["fail"] == 0 else 2


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

    p_cl = sub.add_parser(
        "cleanup", help="서버 목록에 없는 곡의 로컬 파일 치우기 (실행 시 자동)")
    p_cl.add_argument("--dry-run", action="store_true", help="치우지 않고 목록만 보기")
    p_cl.add_argument("--purge", action="store_true",
                      help="_removed 로 옮기지 않고 바로 삭제")

    p_scan = sub.add_parser(
        "scan", help="직접 넣은 WAV 를 찾아 이름 정리 + 기록 + MP3 변환 (실행 시 자동)")
    p_scan.add_argument("--dry-run", action="store_true", help="바꾸지 않고 결과만 보기")
    p_scan.add_argument("--no-mp3", action="store_true", help="MP3 변환은 하지 않기")

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
    if args.cmd == "cleanup":
        songs = _load_library().get("songs", [])
        if not songs:
            print("목록이 없습니다.  sync  를 먼저 실행하세요.")
            return 1
        cleanup_removed(songs, apply=not args.dry_run, purge=args.purge)
        return 0
    if args.cmd == "scan":
        scan_wavs(dry_run=args.dry_run, make_mp3=not args.no_mp3)
        return 0
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
    # 손으로 받아 wav 폴더에 넣어둔 파일이 있으면 정리해서 받아들인다
    scan_wavs(quiet=True)
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
