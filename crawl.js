/* 수강신청 페이지에서 시간표에 필요한 것을 전부 긁어온다.
 *
 *  1. 수강신청 페이지(/ucr/ucreTlsnAplymMngt/index.do)를 로그인한 상태로 연다
 *  2. F12 → Console 에 이 파일 내용을 통째로 붙여넣고 실행
 *  3. 출력된 JSON 을 project_timetable\timetable.json 으로 저장
 *     (자동 복사가 안 되면 콘솔에 copy(timetable) 입력)
 *  4. py make_timetable.py
 *
 * 긁는 것: 신청내역 그리드(과목코드·분반·과목명·강의시간·학점·교수·이수구분·학부),
 *          과목별 강의실(수업계획서 조회 API), 개강일(수강변경기간 첫날)·종강일.
 * 로그인된 세션을 그대로 쓰므로 비밀번호나 쿠키를 다룰 일이 없다.
 */
(async () => {
  const grid = $("#grid2");
  if (!grid.length) {
    console.error("신청내역 그리드(#grid2)를 못 찾았습니다. 수강신청 페이지가 맞는지 확인하세요.");
    return;
  }
  const rows = grid.jqGrid("getRowData");            // 숨김 열까지 전부 들어온다
  if (!rows.length) {
    console.error("신청내역이 비어 있습니다.");
    return;
  }

  // ── 개강일: 수강변경기간 첫날. 페이지 본문에 적혀 있다
  const text = document.body.innerText.replace(/\s+/g, " ");
  const chg = text.match(/수강\s*변경\s*기간[^0-9]{0,30}(20\d{2})\/(\d{2})\/(\d{2})/);
  const start = chg ? `${chg[1]}-${chg[2]}-${chg[3]}` : "";

  // ── 종강일: 개설강좌 그리드에 개강일·종강일 칸이 있으면 가장 많이 나온 쌍을 쓴다
  let end = "";
  const search = $("#grid1");
  if (search.length) {
    const count = {};
    for (const r of search.jqGrid("getRowData")) {
      const days = Object.values(r).filter(v => /^20\d{2}\/\d{2}\/\d{2}$/.test(v)).sort();
      if (days.length >= 2) {
        const key = days[0] + "|" + days[days.length - 1];
        count[key] = (count[key] || 0) + 1;
      }
    }
    const best = Object.keys(count).sort((a, b) => count[b] - count[a])[0];
    if (best) end = best.split("|")[1].replace(/\//g, "-");
  }

  // ── 과목별 강의실: 수업계획서 조회 API 의 INFO 에 "화6B-7B(E7 - 241)" 형태로 들어 있다
  const courses = [];
  for (const r of rows) {
    const body = new URLSearchParams({
      pageNum: "1", pageSize: "10", sortName: "", sortOrder: "",
      commonMenuId: "STUD_TLSN_APLY", commonProgramId: "UcreTlsnAplyMngt", sttsMenuYn: "N",
      conYear: r.SHYY, conTerm: r.SHTM_DCD, conSust: r.ASGN_SUST_CD,
      conCors: r.ASGN_CORS_DCD, conOrgn: r.ORGN_CLSF_DCD,
      conSbjtNo: r.SBJT_NO, conClss: r.CLSS_NO,
    });

    let info = "";
    try {
      const res = await fetch("/ucs/ucseLsnPdocMngt/lecInfo.do", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                   "X-Requested-With": "XMLHttpRequest" },
        body,
      });
      const raw = await res.text();
      try {
        const j = JSON.parse(raw);
        info = j.INFO ?? j.data?.INFO ?? j.result?.INFO ?? "";
        if (!info) info = (JSON.stringify(j).match(/"INFO"\s*:\s*"([^"]*)"/) || [])[1] || "";
      } catch (e) {
        info = (raw.match(/"INFO"\s*:\s*"([^"]*)"/) || [])[1] || "";
      }
    } catch (e) {
      console.warn(r.SBJT_NO, "강의실 조회 실패:", e.message);
    }

    // "화6B-7B(E7 - 241), 목6B-7B(E7 - 241)" → "E7-241" (요일마다 다르면 쉼표로 잇는다)
    const seen = [];
    for (const m of info.matchAll(/\(([^)]*)\)/g)) {
      const room = m[1].trim().replace(/\s+/g, " ").replace(/\s*-\s*/g, "-");
      if (room && !seen.includes(room)) seen.push(room);
    }

    courses.push({
      code: r.SBJT_NO, section: r.CLSS_NO, name: r.SBJT_NM,
      time: r.LEC_TIME, room: seen.join(", "),
      credit: r.PNT, professor: r.PROF_NM,
      category: r.CPTN_DCD_NM, dept: r.ASGN_SUST_NM,
    });
  }

  const payload = { year: rows[0].SHYY, start, end, courses };
  const out = JSON.stringify(payload, null, 2);
  window.timetable = out;                            // 복사에 실패해도 여기 남는다
  console.log(out);
  console.log(`과목 ${courses.length}개, 강의실 ${courses.filter(c => c.room).length}개, ` +
              `개강일 ${start || "못 찾음"}, 종강일 ${end || "못 찾음"}`);

  try {
    await navigator.clipboard.writeText(out);
    console.log("클립보드에 복사했습니다. timetable.json 으로 저장하세요.");
  } catch (e) {
    try {
      copy(out);                                     // DevTools 콘솔 전용 함수
      console.log("copy() 로 클립보드에 복사했습니다. timetable.json 으로 저장하세요.");
    } catch (e2) {
      console.log("자동 복사 실패. 콘솔에  copy(timetable)  을 입력하세요.");
    }
  }
})();
