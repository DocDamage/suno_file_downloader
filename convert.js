/* ============================================================
   Suno - WAV 변환 요청기  (브라우저 콘솔용 / 보조 스크립트)
   ------------------------------------------------------------
   download.py 가 "WAV 가 아직 없다"고 알려준 곡들에 대해
   서버에 WAV 생성을 요청한다.

   사용법:
     1. download.py 실행 후 생긴 needs_wav.json 을 연다
     2. 그 안의 "id" 값들을 아래 IDS 배열에 붙여넣는다
     3. suno.com 로그인 탭의 콘솔에 이 파일 전체를 붙여넣고 Enter
     4. 잠시 기다린 뒤 download.py 를 다시 실행

   ⚠ 주의: WAV 생성 엔드포인트는 Suno 가 공개한 API 가 아니라서
     버전에 따라 경로가 바뀔 수 있다. 이 스크립트는 후보 경로를
     하나씩 시도해 보고 어떤 것이 통했는지 알려준다.
     전부 실패하면 콘솔 출력을 그대로 복사해서 알려주면 된다.
   ============================================================ */
(async () => {
  // ▼▼▼ 여기에 needs_wav.json 의 id 들을 붙여넣으세요 ▼▼▼
  const IDS = [
    // "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  ];
  // ▲▲▲ ---------------------------------------- ▲▲▲

  const API = 'https://studio-api.prod.suno.com';
  const log = (...a) => console.log('%c[suno]', 'color:#7c5cff;font-weight:bold', ...a);
  const err = (...a) => console.error('%c[suno]', 'color:#ff4d4f;font-weight:bold', ...a);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const ids = IDS.length ? IDS : (window.__sunoNeedsWav || []);
  if (!ids.length) {
    err('IDS 배열이 비어 있습니다. needs_wav.json 의 id 들을 붙여넣어 주세요.');
    return;
  }
  const clerk = window.Clerk;
  if (!clerk || !clerk.session) {
    err('suno.com 에 로그인한 탭에서 실행하세요.');
    return;
  }

  // 후보 엔드포인트 (경로, HTTP 메서드, 바디 생성기)
  const CANDIDATES = [
    ['/api/gen/{id}/convert_wav/', 'POST', () => ({})],
    ['/api/gen/{id}/wav/',          'POST', () => ({})],
    ['/api/gen/{id}/wav_file/',     'GET',  null],
    ['/api/edit/convert_wav/',      'POST', (id) => ({ clip_id: id })],
    ['/api/clip/{id}/wav/',         'POST', () => ({})],
  ];

  async function call(pathTpl, method, bodyFn, id) {
    const token = await clerk.session.getToken();
    const init = {
      method,
      headers: { Authorization: 'Bearer ' + token },
      credentials: 'include',
    };
    if (bodyFn) {
      init.headers['Content-Type'] = 'text/plain;charset=UTF-8';
      init.body = JSON.stringify(bodyFn(id));
    }
    const res = await fetch(API + pathTpl.replace('{id}', id), init);
    let body = null;
    try { body = await res.clone().json(); } catch { body = (await res.text()).slice(0, 200); }
    return { status: res.status, ok: res.ok, body };
  }

  // ---- 1) 첫 번째 id 로 어떤 엔드포인트가 통하는지 탐색 ----
  let winner = null;
  log(`엔드포인트 탐색 중... (샘플 id: ${ids[0]})`);
  for (const [tpl, method, bodyFn] of CANDIDATES) {
    try {
      const r = await call(tpl, method, bodyFn, ids[0]);
      log(`  ${method} ${tpl} -> ${r.status}`, r.body);
      if (r.ok) { winner = [tpl, method, bodyFn]; break; }
    } catch (e) {
      log(`  ${method} ${tpl} -> 예외: ${e.message}`);
    }
    await sleep(300);
  }

  if (!winner) {
    err('통하는 WAV 변환 엔드포인트를 찾지 못했습니다.');
    err('위의 로그 전체를 복사해서 알려주면 경로를 맞춰 드립니다.');
    err('또는: 곡 하나에서 수동으로 Download > WAV 를 누르면서 F12 > Network 탭에 뜨는 요청 URL을 확인해 주세요.');
    return;
  }

  const [tpl, method, bodyFn] = winner;
  log(`✅ 사용할 엔드포인트: ${method} ${tpl}`);

  // ---- 2) 나머지 전부에 대해 변환 요청 ----
  let ok = 0;
  const failed = [];
  for (let i = 0; i < ids.length; i++) {
    const id = ids[i];
    try {
      const r = await call(tpl, method, bodyFn, id);
      if (r.ok) { ok++; log(`[${i + 1}/${ids.length}] ✓ ${id}`); }
      else { failed.push(id); log(`[${i + 1}/${ids.length}] ✗ ${id} -> ${r.status}`, r.body); }
    } catch (e) {
      failed.push(id);
      log(`[${i + 1}/${ids.length}] ✗ ${id} -> ${e.message}`);
    }
    await sleep(500); // 서버 부담 줄이기
  }

  log(`\n완료 — 요청 성공 ${ok}곡, 실패 ${failed.length}곡`);
  if (failed.length) console.log('실패 id:', failed);
  log('변환은 서버에서 비동기로 처리됩니다. 몇 분 기다린 뒤 download.py 를 다시 실행하세요.');
})();
