"""수강신청 자료 → Google 캘린더용 .ics

    1) 수강신청 페이지 콘솔에서 crawl.js 를 실행해 timetable.json 을 저장한다
    2) run_timetable.bat (또는 py make_timetable.py)

한 번 실행하면 신청한 과목 전부가 들어간다. 년도·학기·수업 시작일은 긁어온 값을
기본값으로 물어보고, 엔터만 치면 그대로 쓴다.

중간에 courses.csv 를 남긴다. 색을 지정하거나 강의실을 손보고 싶으면 거기를 고치고
다시 실행하면 된다. 과목코드+분반으로 찾아 유지한다. 특정 과목을 빼고 싶으면
'제외' 칸에 Y 를 적으면 되지만, 한두 개면 캘린더에서 지우는 편이 빠르다.

timetable.json 이 없으면 수강신청 페이지 소스를 저장한 HTML 에서 직접 읽는다.
표가 여러 개라도 '취소' 버튼이 있는 표(= 내 신청내역)를 골라낸다.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser

TZID = "Asia/Seoul"
KST = timezone(timedelta(hours=9))

WEEKDAY_KO = "월화수목금토일"
DAY_OFFSET = {d: i for i, d in enumerate(WEEKDAY_KO)}
BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

# 학기 → UID·파일명에 쓰는 이름. 1/2 외에 계절학기도 받는다
SEASON = {"1": "spring", "2": "fall", "여름": "summer", "겨울": "winter"}

# 색을 비워둔 과목에 파일 순서대로 배정한다. Google 캘린더 색 11가지에 맞춰 놨고,
# 과목이 더 많으면 앞에서부터 다시 쓴다
PALETTE = ["tomato", "salmon", "darkorange", "gold", "yellowgreen", "seagreen",
           "steelblue", "royalblue", "mediumpurple", "purple", "slategray"]

# "월10:30-12:00" 한 덩어리. 구분자는 쉼표든 공백이든 상관없다
SLOT_RE = re.compile(r"([월화수목금토일])\s*(\d{1,2}):(\d{2})\s*[-~]\s*(\d{1,2}):(\d{2})")

CODE_RE = re.compile(r"^[A-Za-z]{2,8}\d{2,4}[A-Za-z]?$")   # BE204, CHEM203, HSS301a
SECTION_RE = re.compile(r"^\d{1,3}$")                       # 01
CREDIT_RE = re.compile(r"^\d{1,2}(\.\d+)?$")                # 3.0
# 숨김 열에 섞여 있는 순번·연도·내부 코드. 설명에 들어가면 안 된다
JUNK_RE = re.compile(r"^(\d+|[A-Z]{2,}[\d.]*)$")
DATE_CELL_RE = re.compile(r"^(20\d{2})/(\d{2})/(\d{2})$")   # 개강일 칸 2026/08/24
# "수강변경기간 : 2026/08/24 (09:00) ~ 2026/09/04 (23:59)" — 이 첫날이 개강일이다
CHANGE_PERIOD_RE = re.compile(r"수강\s*변경\s*기간[^0-9]{0,30}(20\d{2})/(\d{2})/(\d{2})")

# 신청내역 표에만 있는 버튼. 검색결과 표(detailView)와 구별하는 표시다
ENROLLED_MARK = "deleteapply"

CSV_COLUMNS = ["과목코드", "분반", "과목명", "시간", "장소", "색", "제외", "설명"]
# '제외' 칸에 이 중 하나를 적으면 그 과목은 .ics 에 넣지 않는다
EXCLUDE_MARKS = {"Y", "YES", "O", "1", "TRUE", "제외", "V"}


# ── HTML 에서 과목 뽑기 ────────────────────────────────────────────────

class TableParser(HTMLParser):
    """표별로 행을 모은다. 표가 중첩돼도 가장 안쪽 표에 넣는다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = {}        # 표 id → 행 목록
        self.enrolled = set()   # '취소' 버튼이 있던 표 id
        self._stack = []        # 표 id 스택
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self._stack.append(attrs.get("id") or f"표{len(self.tables) + 1}")
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        if self._stack and ENROLLED_MARK in attrs.get("onclick", "").lower():
            self.enrolled.add(self._stack[-1])

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables.setdefault(self._stack[-1], []).append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data.replace("\xa0", " "))


def find_course(cells):
    """표 한 행 → 과목 정보. 과목으로 안 보이면 None.

    열 위치가 아니라 내용으로 찾는다. 숨김 열이 몇 개 끼어 있어도 상관없다.
    """
    code_at = next((i for i, c in enumerate(cells) if CODE_RE.match(c)), None)
    time_at = next((i for i, c in enumerate(cells) if SLOT_RE.search(c)), None)
    if code_at is None or time_at is None:
        return None

    i = code_at + 1
    section = cells[i] if i < len(cells) and SECTION_RE.match(cells[i]) else ""
    if section:
        i += 1
    name = cells[i] if i < len(cells) else ""
    if not name:
        return None

    # 과목명과 시간 사이에서 학점(3.0)을 찾는다. 그 다음 칸이 교수다
    credit_at = next((j for j in range(i + 1, len(cells)) if CREDIT_RE.match(cells[j])), None)
    credit = cells[credit_at] if credit_at is not None else ""
    professor = cells[credit_at + 1] if credit_at is not None and credit_at + 1 < len(cells) else ""
    if JUNK_RE.match(professor):
        professor = ""

    # 과목코드 앞에 남는 칸(학부/이수구분 등). 순번·연도·내부 코드는 버린다
    desc = [c for c in cells[:code_at] if c and not JUNK_RE.match(c)]
    if section:
        desc.append(f"분반 {section}")
    if credit:
        desc.append(f"{credit.rstrip('0').rstrip('.')}학점")
    if professor:
        desc.append(f"담당교수 {professor}")

    return {
        "과목코드": cells[code_at],
        "분반": section,
        "과목명": name,          # 과목코드는 따로 있으니 이름에 붙이지 않는다
        "시간": cells[time_at],
        "장소": "",
        "색": "",
        "제외": "",
        "설명": " / ".join(desc),
    }


def scan_table(rows):
    """행 목록 → (과목 목록, 시간이 없어 못 넣은 과목).

    시간표에 못 넣는 과목(논문·인턴십처럼 시간이 안 적힌 것)은 조용히 버리지 않고
    돌려줘서 화면에 알린다.
    """
    courses, no_time = [], []
    for cells in rows:
        course = find_course(cells)
        if course:
            courses.append(course)
            continue
        code_at = next((i for i, c in enumerate(cells) if CODE_RE.match(c)), None)
        if code_at is not None:
            name = next((c for c in cells[code_at + 1:] if c and not JUNK_RE.match(c)), "")
            no_time.append(f"{cells[code_at]} {name}".strip())
    return courses, no_time


def pick_table(parser, want):
    """어느 표가 '내 신청내역'인지 고른다."""
    found = {tid: scan_table(rows) for tid, rows in parser.tables.items()}
    # 시간이 있는 과목이 실제로 든 표만 후보다. 수업계획서의 평가비율 표처럼 과목코드만
    # 스치는 표가 걸려들면 시간표를 통째로 덮어쓰게 된다
    found = {tid: v for tid, v in found.items() if v[0]}
    if not found:
        return None       # 과목 표가 없는 파일. 다른 후보를 보면 된다

    def summary():
        return ", ".join(f"{t}({len(v[0]) + len(v[1])}과목)" for t, v in found.items())

    if want:
        if want not in found:
            sys.exit(f"--table {want} 에 과목이 없음. 후보: {summary()}")
        return (want, *found[want])

    enrolled = [t for t in found if t in parser.enrolled]
    if len(enrolled) == 1:
        return (enrolled[0], *found[enrolled[0]])
    if len(found) == 1:
        tid = next(iter(found))
        if tid not in parser.enrolled:
            # 신청내역이 비어 있으면 검색결과 표가 유일한 후보로 남는다. 그건 내 시간표가 아니다
            print(f"  주의: '취소' 버튼이 있는 신청내역 표를 못 찾아 '{tid}' 를 씁니다. "
                  f"검색결과 표일 수 있으니 아래 목록을 확인하세요.", file=sys.stderr)
        return (tid, *found[tid])
    sys.exit(f"표가 여러 개라 어느 것이 신청내역인지 모르겠음. --table 로 지정하세요.\n  후보: {summary()}")


def parse_file(path):
    """HTML 파일 → (원문, 표를 다 읽은 파서)"""
    with open(path, encoding="utf-8", errors="replace") as f:
        source = f.read()
    parser = TableParser()
    parser.feed(source)
    return source, parser


def read_html(path, want_table):
    """과목 표가 없는 파일이면 None. 후보가 여럿일 때 걸러내려는 것이다."""
    source, parser = parse_file(path)
    picked = pick_table(parser, want_table)
    if picked is None:
        return None
    table_id, courses, no_time = picked
    return table_id, courses, no_time, detect_dates(source, parser.tables)


def read_export(path):
    """crawl.js 가 만든 JSON → (CSV 행 목록, (개강일, 종강일))"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    for c in data.get("courses", []):
        credit = str(c.get("credit", "")).rstrip("0").rstrip(".")
        desc = [c.get("dept", ""), c.get("category", "")]
        if c.get("section"):
            desc.append(f"분반 {c['section']}")
        if credit:
            desc.append(f"{credit}학점")
        if c.get("professor"):
            desc.append(f"담당교수 {c['professor']}")
        rows.append({
            "과목코드": c.get("code", ""), "분반": c.get("section", ""),
            "과목명": c.get("name", ""), "시간": c.get("time", ""),
            "장소": c.get("room", ""), "색": "", "제외": "",
            "설명": " / ".join(x for x in desc if x),
        })

    span = tuple(date.fromisoformat(data[k]) if data.get(k) else None
                 for k in ("start", "end"))
    return rows, span


def load_span(args, here):
    """ics 단계에서 개강일·종강일 '기본값'만 HTML 에서 얻는다.

    과목은 CSV 에서만 읽는다. HTML 이 없으면 그냥 직접 입력받으면 되므로 조용히 넘어간다.
    """
    for path in ([args.html] if args.html else find_html(here)):
        if not os.path.exists(path):
            continue
        source, parser = parse_file(path)
        span = detect_dates(source, parser.tables)
        if span != (None, None):
            return span
    return (None, None)


def detect_dates(source, tables):
    """(개강일, 종강일). 입력 기본값으로만 쓰므로 틀려도 사용자가 고칠 수 있다.

    개강일은 '수강변경기간' 첫날이 1순위다. 수강변경기간이 개강일에 시작하기 때문이다.
    그 문구가 없으면 강좌 표로 물러난다. 표에서는 한 행에 날짜 칸이 둘 이상인 것을
    (개강일, 종강일) 후보로 보고 가장 많이 나온 쌍을 고른다. 페이지 구석의 오늘 날짜
    같은 것에 끌려가지 않게 하려는 것이다. 종강일은 표에서만 얻는다.
    """
    pairs = {}
    for rows in tables.values():
        for cells in rows:
            days = [date(int(m[1]), int(m[2]), int(m[3]))
                    for m in map(DATE_CELL_RE.match, cells) if m]
            if len(days) >= 2:
                pair = (min(days), max(days))
                pairs[pair] = pairs.get(pair, 0) + 1
    row_start, row_end = max(pairs, key=pairs.get) if pairs else (None, None)

    text = " ".join(re.sub(r"<[^>]+>", " ", source).split())
    m = CHANGE_PERIOD_RE.search(text)
    return (date(int(m[1]), int(m[2]), int(m[3])) if m else row_start, row_end)


# ── courses.csv ────────────────────────────────────────────────────────

def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, courses):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:   # BOM: 엑셀에서 안 깨지게
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(courses)


def merge_csv(courses, path):
    """포털에 없는 값(강의실·색·제외)을 기존 CSV 에서 가져온다.

    포털을 다시 읽어도 사람이 적어둔 것은 살아남아야 한다. (과목코드, 분반) 으로 찾는다.
    """
    if not os.path.exists(path):
        return 0
    old = {(r.get("과목코드", ""), r.get("분반", "")): r for r in read_csv(path)}
    kept = 0
    for c in courses:
        prev = old.get((c["과목코드"], c["분반"]))
        if prev:
            for col in ("장소", "색", "제외"):
                c[col] = c[col] or prev.get(col, "")
            kept += 1
    return kept


def parse_schedule(text):
    """'월10:30-12:00, 수10:30-12:00' → [(요일offset, '10:30', '12:00'), ...]"""
    slots = []
    for day, h1, m1, h2, m2 in SLOT_RE.findall(text):
        begin, end = f"{int(h1):02d}:{m1}", f"{int(h2):02d}:{m2}"
        if int(h1) > 23 or int(h2) > 23:
            raise ValueError(f"시간 범위가 이상함: {day}{begin}-{end}")
        if end <= begin:
            raise ValueError(f"끝나는 시각이 시작보다 빠름: {day}{begin}-{end}")
        slots.append((DAY_OFFSET[day], begin, end))

    if not slots:
        raise ValueError(f"시간을 못 읽음: {text!r} (예: '월10:30-12:00, 수10:30-12:00')")
    days = [s[0] for s in slots]
    if len(days) != len(set(days)):
        raise ValueError(f"같은 요일이 두 번 나옴: {text!r}")
    return slots


def code_set(text):
    """'chem303, be204' → {'CHEM303', 'BE204'}"""
    return {c.strip().upper() for c in text.split(",") if c.strip()}


def prepare(rows, excluded, include_all=False, only=()):
    """CSV 행 → 이벤트 만들 수 있는 과목 목록. 오류는 모아서 한 번에 보여준다."""
    courses, errors, dropped = [], [], []
    for i, row in enumerate(rows, start=2):     # 2 = 헤더 다음 줄 (엑셀 행 번호)
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        if not row.get("과목코드") or not row.get("과목명"):
            errors.append(f"{i}행: 과목코드와 과목명은 비울 수 없음")
            continue
        # --only 를 주면 그 과목만. 아니면 '제외' 칸과 --exclude 를 따르되 --all 이면 무시
        code = row["과목코드"].upper()
        if only:
            keep = code in only
        else:
            keep = include_all or not (row.get("제외", "").upper() in EXCLUDE_MARKS
                                       or code in excluded)
        if not keep:
            dropped.append(row["과목코드"])
            continue
        try:
            row["slots"] = parse_schedule(row.get("시간", ""))
        except ValueError as e:
            errors.append(f"{i}행 [{row['과목코드']}]: {e}")
            continue
        courses.append(row)

    if errors:
        sys.exit(f"courses.csv 오류 {len(errors)}건\n  " + "\n  ".join(errors))
    if not courses:
        sys.exit("만들 과목이 없음")
    for n, c in enumerate(courses):
        c["색"] = c["색"] or PALETTE[n % len(PALETTE)]
    return courses, dropped


# ── .ics 만들기 ────────────────────────────────────────────────────────

def escape(text):
    """RFC 5545 TEXT 값 이스케이프. 과목명의 쉼표와 값 안의 줄바꿈이 여기 걸린다."""
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return re.sub(r"\r\n|[\r\n]", "\\\\n", text)      # 줄바꿈은 리터럴 \n 으로


def fold(line):
    """RFC 5545 줄 접기. 한 줄 75옥텟 제한이라 긴 과목명은 잘라 넘겨야 한다.

    이어지는 줄은 공백 한 칸으로 시작한다. 한글이 쪼개지지 않게 글자 단위로 센다.
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        raw = ch.encode("utf-8")
        limit = 75 if not out else 74      # 이어지는 줄은 맨 앞 공백이 1옥텟을 먹는다
        if len(cur) + len(raw) > limit:
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += raw
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def stamp(day, hhmm):
    """date + 'HH:MM' → 'YYYYMMDDTHHMMSS'"""
    return f"{day:%Y%m%d}T{hhmm.replace(':', '')}00"


def meeting_date(sem, week, offset):
    """week 주차의 요일 offset 수업 날짜.

    시작일이 월요일이 아니어도 되게, 시작일 당일부터 세어 처음 오는 그 요일을 잡는다.
    """
    first = sem["start"] + timedelta(days=(offset - sem["start"].weekday()) % 7)
    return first + timedelta(days=(week - 1) * 7)


def group_by_time(slots):
    """같은 시간대끼리 묶는다 → [((시작, 끝), [요일offset, ...]), ...]

    요일마다 시간이 다르면 묶음이 갈라지고, 묶음 하나가 VEVENT 하나가 된다.
    """
    groups = {}
    for offset, begin, end in slots:
        groups.setdefault((begin, end), []).append(offset)
    return [(t, sorted(offsets)) for t, offsets in groups.items()]


def event_lines(course, sem, dtstamp):
    lines = []
    groups = group_by_time(course["slots"])
    for n, ((begin, end), offsets) in enumerate(groups):
        # 시간대가 갈라진 경우에만 UID 에 꼬리표를 붙인다
        uid = "-".join(filter(None, [course["과목코드"].lower(), course["분반"],
                                     sem["slug"], f"g{n + 1}" if len(groups) > 1 else "",
                                     sem["uid_suffix"]]))
        first = meeting_date(sem, 1, offsets[0])

        # 건너뛰는 주 중 반복 범위 안에 있는 것만 EXDATE 로 뺀다
        excluded = [stamp(meeting_date(sem, w, off), begin)
                    for w in sem["skip"] if w < sem["last_week"]
                    for off in offsets]

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@timetable",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID={TZID}:{stamp(first, begin)}",
            f"DTEND;TZID={TZID}:{stamp(first, end)}",
            f"RRULE:FREQ=WEEKLY;BYDAY={','.join(BYDAY[o] for o in offsets)}"
            f";UNTIL={sem['until']}",
        ]
        if excluded:
            lines.append(f"EXDATE;TZID={TZID}:{','.join(excluded)}")
        lines += [
            f"SUMMARY:{escape(course['과목명'])}",
            f"COLOR:{course['색']}",
        ]
        if course["장소"]:
            lines.append(f"LOCATION:{escape(course['장소'])}")
        if course["설명"]:
            lines.append(f"DESCRIPTION:{escape(course['설명'])}")
        lines += [
            # 기본 알림 억제. Google 은 이걸 무시하고 캘린더 기본 알림을 붙일 수 있다.
            "BEGIN:VALARM",
            "ACTION:NONE",
            "TRIGGER:19760401T005545Z",
            "END:VALARM",
            "END:VEVENT",
        ]
    return lines


def build(courses, sem):
    dtstamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}Z"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Class Timetable//KO",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{sem['calname']}",
        f"X-WR-TIMEZONE:{TZID}",
        "BEGIN:VTIMEZONE",
        f"TZID:{TZID}",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0900",
        "TZOFFSETTO:+0900",
        "TZNAME:KST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for course in courses:
        lines += event_lines(course, sem, dtstamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# ── 물어보기 ───────────────────────────────────────────────────────────

def ask(prompt, default):
    """엔터만 치면 기본값. 입력이 없으면(배치 파일·파이프) 묻지 않고 기본값을 쓴다."""
    if not sys.stdin.isatty():
        print(f"{prompt} [{default}] → {default}")
        return str(default)
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:      # < nul 로 실행하면 여기로 온다. 프롬프트는 이미 찍혔다
        print(f"→ {default}")
        return str(default)
    return answer or str(default)



def guess_term(day):
    """개강일의 달로 학기를 짐작한다. 물어볼 때 기본값으로만 쓰고, 틀리면 사람이 고친다.

        2~3월 → 1학기    6~7월 → 여름학기    8~9월 → 2학기    12~1월 → 겨울학기

    4·5·10·11월은 학기 중이라 개강일이 될 일이 없지만, 값은 있어야 하므로 그 달이
    속한 학기로 둔다. 포털의 학기 코드(SHTM_DCD=CMN17.20)는 뜻을 확인할 수 없어 쓰지 않는다.
    """
    return {2: "1", 3: "1", 4: "1", 5: "1",
            6: "여름", 7: "여름",
            8: "2", 9: "2", 10: "2", 11: "2",
            12: "겨울", 1: "겨울"}[day.month]


def resolve_semester(args, span):
    """년도·학기·시작일을 정한다. 인자로 준 건 묻지 않는다."""
    detected_start, detected_end = span

    year = args.year or ask("년도", (detected_start or date.today()).year)
    term = args.term or ask("학기 (1/2/여름/겨울)",
                            guess_term(detected_start) if detected_start else "2")
    if term not in SEASON:
        sys.exit(f"학기는 {'/'.join(SEASON)} 중 하나여야 함: {term}")

    default_start = detected_start.isoformat() if detected_start else f"{year}-03-02"
    raw = args.start or ask("수업 시작일 (YYYY-MM-DD)", default_start)
    try:
        start = date.fromisoformat(raw)
    except ValueError:
        sys.exit(f"날짜 형식이 틀림: {raw} (예: 2026-08-24)")

    weekday = WEEKDAY_KO[start.weekday()]
    print(f"  → {start} {weekday}요일 시작"
          + ("" if start.weekday() == 0 else ", 주차는 이 날부터 7일씩 끊습니다"))

    # 종강일이 있으면 몇 주짜리 학기인지 거기서 센다
    weeks = args.weeks
    if weeks is None:
        weeks = (detected_end - start).days // 7 + 1 if detected_end and detected_end > start else 16
    try:
        skip = sorted({int(w) for w in args.skip.split(",") if w.strip()})
    except ValueError:
        sys.exit(f"--skip 은 주차 번호만 쉼표로 적는다: {args.skip} (예: 8,16)")
    if any(w < 1 or w > weeks for w in skip):
        sys.exit(f"--skip 이 1~{weeks} 범위를 벗어남: {args.skip}")

    teaching = [w for w in range(1, weeks + 1) if w not in skip]
    if not teaching:
        sys.exit("수업하는 주가 하나도 없음")

    # 마지막 수업 주의 끝(7일째) 23:59:59 KST 를 UTC 로. 그 주 수업까지만 포함된다
    end_local = datetime.combine(start + timedelta(days=(teaching[-1] - 1) * 7 + 6),
                                 time(23, 59, 59), KST)
    season = SEASON[term]
    return {
        "start": start,
        "weeks": weeks,
        "skip": skip,
        "teaching": teaching,
        "last_week": teaching[-1],
        "until": f"{end_local.astimezone(timezone.utc):%Y%m%dT%H%M%S}Z",
        "calname": f"{year}학년도 {term}학기 시간표",
        "slug": f"{year}{season}",
        "uid_suffix": args.uid_suffix.strip().lower(),
        "outname": f"timetable_{year}_{season}.ics",
    }


# ── 실행 ───────────────────────────────────────────────────────────────

def downloads_dir():
    """다운로드 폴더. 다른 드라이브로 옮겨져 있을 수 있어 레지스트리에서 찾는다."""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
    except (ImportError, OSError):
        return os.path.join(os.path.expanduser("~"), "Downloads")


def find_html(here):
    """다운로드\\시간표 폴더와 스크립트 폴더의 HTML 후보를 최근 것부터 돌려준다.

    수업계획서처럼 과목 표가 없는 파일도 같이 걸리므로, 실제로 쓸 파일은 읽어 보고
    고른다. 그래서 하나가 아니라 목록을 준다.
    """
    candidates = []
    for folder in (os.path.join(downloads_dir(), "시간표"), here):
        if os.path.isdir(folder):
            candidates += [os.path.join(folder, f) for f in os.listdir(folder)
                           if f.lower().endswith((".html", ".htm", ".txt"))]
    candidates = [c for c in candidates if os.path.getsize(c) > 0]
    return sorted(candidates, key=os.path.getmtime, reverse=True)


def parse_args(argv):
    p = argparse.ArgumentParser(description="수강신청 페이지 자료 → Google 캘린더 .ics")
    p.add_argument("--json", default="timetable.json",
                   help="crawl.js 로 긁은 파일 (기본: timetable.json)")
    p.add_argument("--csv", default="courses.csv", help="과목 CSV (기본: courses.csv)")
    p.add_argument("--out", help="출력 .ics (기본: timetable_년도_학기.ics)")
    p.add_argument("--year", help="년도 (없으면 물어봄)")
    p.add_argument("--term", help="학기 1/2/여름/겨울 (없으면 물어봄)")
    p.add_argument("--start", help="수업 시작일 YYYY-MM-DD (없으면 물어봄)")
    p.add_argument("--weeks", type=int, help="전체 주차 수 (기본: 종강일에서 계산, 없으면 16)")
    p.add_argument("--skip", default="8,16", help="수업 없는 주차, 쉼표 구분 (기본: 8,16)")
    # 아래는 안 써도 되는 확장용. 기본은 신청한 과목 전부를 넣는다
    p.add_argument("--only", default="", help="이 과목코드만 넣는다, 쉼표 구분")
    p.add_argument("--exclude", default="", help="이 과목코드를 뺀다, 쉼표 구분")
    p.add_argument("--all", action="store_true", help="CSV '제외' 칸까지 무시하고 전부 넣는다")
    p.add_argument("--uid-suffix", default="",
                   help="UID 뒤에 붙일 꼬리표. 이미 가져간 캘린더가 예전 일정을 기억할 때 "
                        "값을 바꾸면(예: v2) 완전히 새 일정으로 들어간다")
    p.add_argument("--html", help="예비 경로: 페이지 소스 파일에서 직접 읽는다")
    p.add_argument("--table", help="예비 경로에서 쓸 표의 id")
    return p.parse_args(argv)


def ingest(args, here):
    """과목 목록을 어디서 읽을지 정한다. crawl.js JSON 이 1순위, 페이지 HTML 이 예비다."""
    path = args.json if os.path.isabs(args.json) else os.path.join(here, args.json)
    if os.path.exists(path):
        rows, span = read_export(path)
        if not rows:
            sys.exit(f"{path} 에 과목이 없습니다. crawl.js 를 다시 실행하세요.")
        print(f"{path} → {len(rows)}과목")
        return rows, span

    for candidate in ([args.html] if args.html else find_html(here)):
        if not os.path.exists(candidate):
            continue
        found = read_html(candidate, args.table)
        if found:
            table_id, rows, no_time, span = found
            print(f"{candidate} [{table_id}] → {len(rows)}과목")
            for name in no_time:      # 시간이 안 적힌 과목은 시간표에 못 넣는다
                print(f"  시간 정보가 없어 제외: {name[:50]}")
            return rows, span

    return None, (None, None)


def main(argv=None):
    args = parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    csv_path = args.csv if os.path.isabs(args.csv) else os.path.join(here, args.csv)

    rows, span = ingest(args, here)
    if rows:
        kept = merge_csv(rows, csv_path)      # 손으로 적어둔 색·제외는 살린다
        write_csv(csv_path, rows)
        print(f"  {os.path.basename(csv_path)} 갱신"
              + (f", 적어둔 색·제외 {kept}개 유지" if kept else ""))
    elif os.path.exists(csv_path):
        rows = read_csv(csv_path)
        print(f"{csv_path} → {len(rows)}과목 (새로 긁은 자료가 없어 CSV 그대로 씁니다)")
    else:
        sys.exit("긁어온 자료도 CSV 도 없습니다.\n"
                 "수강신청 페이지 콘솔에서 crawl.js 를 실행해 timetable.json 을 만드세요.")

    sem = resolve_semester(args, span)
    courses, dropped = prepare(rows, code_set(args.exclude),
                               include_all=args.all, only=code_set(args.only))
    out = args.out or os.path.join(here, sem["outname"])
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(build(courses, sem))

    last = sem["start"] + timedelta(days=(sem["last_week"] - 1) * 7 + 6)   # UNTIL 과 같은 기준
    print()
    print(out)
    print(f"{sem['calname']} — {len(courses)}과목 / 수업 {len(sem['teaching'])}주"
          + (f" (제외: {', '.join(dropped)})" if dropped else ""))
    print(f"{sem['start']:%Y-%m-%d}({WEEKDAY_KO[sem['start'].weekday()]}) 시작, "
          f"{sem['weeks']}주 중 {sem['skip']}주차 제외, 마지막 수업 주 {last:%Y-%m-%d}까지")
    for c in courses:
        slots = " ".join(f"{WEEKDAY_KO[o]}{b}-{e}" for o, b, e in c["slots"])
        print(f"  {c['과목코드']:<9} {c['과목명'][:36]}  {slots}  {c['장소'] or '강의실 미입력'}")
    if any(not c["장소"] for c in courses):
        print(f"\n강의실이 빈 과목이 있습니다. {os.path.basename(csv_path)} 의 '장소' 칸에 "
              f"적으면 다음 실행부터 유지됩니다.")


if __name__ == "__main__":
    main()
