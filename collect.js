/* ============================================================
   Suno - Liked 곡 목록 수집기  (브라우저 콘솔용)
   ------------------------------------------------------------
   사용법:
     1. https://suno.com 에 로그인한 탭을 연다
     2. F12 -> Console 탭
     3. 이 파일 내용을 전부 복사해서 붙여넣고 Enter
     4. 끝나면 suno_liked.json 이 다운로드 폴더에 저장됨
   ============================================================ */
(async () => {
  const API = 'https://studio-api.prod.suno.com';
  const MAX_PAGES = 1000;      // 안전장치
  const PAGE_DELAY_MS = 120;   // 레이트리밋 회피용 간격

  const log = (...a) => console.log('%c[suno]', 'color:#7c5cff;font-weight:bold', ...a);
  const err = (...a) => console.error('%c[suno]', 'color:#ff4d4f;font-weight:bold', ...a);

  if (!location.hostname.endsWith('suno.com')) {
    err('suno.com 페이지에서 실행해 주세요. (현재: ' + location.hostname + ')');
    return;
  }
  const clerk = window.Clerk;
  if (!clerk || !clerk.session) {
    err('로그인 세션을 찾을 수 없습니다. suno.com 에 로그인한 뒤 페이지를 새로고침하고 다시 실행하세요.');
    return;
  }

  const getToken = () => clerk.session.getToken();

  // ---- 인증 GET (토큰 만료/레이트리밋 자동 재시도) ----
  async function apiGet(path) {
    let lastErr;
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        const token = await getToken();
        const res = await fetch(API + path, {
          headers: { Authorization: 'Bearer ' + token },
          credentials: 'include',
        });
        if (res.ok) return await res.json();
        if (res.status === 401 || res.status === 429 || res.status >= 500) {
          lastErr = new Error(res.status + ' ' + res.statusText);
          await sleep(700 * (attempt + 1));
          continue;
        }
        throw new Error(res.status + ' ' + res.statusText + ' @ ' + path);
      } catch (e) {
        lastErr = e;
        await sleep(700 * (attempt + 1));
      }
    }
    throw lastErr;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 응답 형태가 배열이거나 {clips:[...]} 형태일 수 있어 둘 다 처리
  const asClips = (data) =>
    Array.isArray(data) ? data
      : Array.isArray(data && data.clips) ? data.clips
      : Array.isArray(data && data.results) ? data.results
      : [];

  // ---- 1) 서버측 liked 필터가 먹는지 먼저 확인 ----
  let likedParam = null;
  for (const p of ['is_liked=true', 'liked=true']) {
    try {
      const probe = asClips(await apiGet('/api/feed/v2?' + p + '&page=0'));
      if (probe.length && probe.every((c) => c.is_liked === true)) {
        likedParam = p;
        log('서버측 liked 필터 사용 가능:', p);
        break;
      }
    } catch (e) { /* 무시하고 다음 후보 */ }
  }
  if (!likedParam) log('서버측 liked 필터를 못 찾았습니다. 라이브러리 전체를 훑어서 직접 걸러냅니다.');

  // ---- 2) 페이지네이션 수집 ----
  const seen = new Map();
  let emptyStreak = 0;

  for (let page = 0; page < MAX_PAGES; page++) {
    const qs = '/api/feed/v2?page=' + page + (likedParam ? '&' + likedParam : '');
    let clips;
    try {
      clips = asClips(await apiGet(qs));
    } catch (e) {
      err('page ' + page + ' 실패, 중단합니다:', e.message);
      break;
    }
    if (clips.length === 0) break;

    let added = 0;
    for (const c of clips) {
      if (!c || !c.id) continue;
      if (seen.has(c.id)) continue;
      seen.set(c.id, c);
      added++;
    }

    const likedSoFar = [...seen.values()].filter((c) => c.is_liked === true).length;
    log(`page ${page}: +${added}곡 (누적 ${seen.size}곡, liked ${likedSoFar}곡)`);

    // 새 곡이 전혀 없는 페이지가 연속 2번이면 끝으로 간주
    emptyStreak = added === 0 ? emptyStreak + 1 : 0;
    if (emptyStreak >= 2) break;

    await sleep(PAGE_DELAY_MS);
  }

  // ---- 3) liked 만 추리기 ----
  const liked = [...seen.values()].filter((c) => c.is_liked === true);

  if (seen.size === 0) {
    err('곡을 하나도 가져오지 못했습니다. 로그인 상태를 확인하세요.');
    return;
  }
  if (liked.length === 0) {
    err(`라이브러리 ${seen.size}곡을 훑었지만 is_liked=true 인 곡이 없습니다.`);
    console.log('첫 번째 곡의 원본 데이터입니다. 필드 이름이 바뀌었을 수 있으니 확인해 주세요:');
    console.log(seen.values().next().value);
    return;
  }

  // ---- 4) 다운로드용 레코드로 변환 ----
  const records = liked.map((c) => ({
    id: c.id,
    title: (c.title || '').trim() || 'untitled',
    created_at: c.created_at || null,
    duration: (c.metadata && c.metadata.duration) || null,
    tags: (c.metadata && c.metadata.tags) || null,
    model: c.model_name || c.major_model_version || null,
    image_url: c.image_url || ('https://cdn2.suno.ai/image_' + c.id + '.jpeg'),
    wav_url: 'https://cdn1.suno.ai/' + c.id + '.wav',
    mp3_url: c.audio_url || ('https://cdn1.suno.ai/' + c.id + '.mp3'),
  }));

  const payload = {
    exported_at: new Date().toISOString(),
    scanned_total: seen.size,
    liked_count: records.length,
    server_side_filter: likedParam,
    songs: records,
  };

  // ---- 5) JSON 파일로 저장 ----
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'suno_liked.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);

  window.__sunoLiked = payload; // 콘솔에서 다시 확인하고 싶을 때
  log(`✅ 완료: Liked ${records.length}곡 (전체 ${seen.size}곡 중). suno_liked.json 저장됨.`);
  console.table(records.slice(0, 10).map((r) => ({ title: r.title, id: r.id })));
})();
