import os
import re
import sys
import time
from urllib.parse import quote, urljoin
import requests

from main import (
    HTTP_HEADERS, SUMMARY_GUIDE, USE_GEMINI_IMAGES,
    _generate, download_image_parts, send_discord_report,
)

# ==========================================
# 1. 설정
# ==========================================
STUDY_REPO = "Meet-Coder-Study/book-system-design-interview"
STUDY_BRANCH = "master"
CHAPTERS = {
    1: "사용자 수에 따른 규모 확장성",
    2: "개략적인 규모 추정",
    3: "시스템 설계 면접 공략법",
    4: "처리율 제한 장치의 설계",
    5: "안정 해시 설계",
    6: "키-값 저장소 설계",
    7: "분산 시스템을 위한 유일 ID 생성기 설계",
    8: "URL 단축기 설계",
    9: "웹 크롤러 설계",
    10: "알림 시스템 설계",
    11: "뉴스 피드 시스템 설계",
    12: "채팅 시스템 설계",
    13: "검색어 자동완성 시스템",
    14: "유튜브 설계",
    15: "구글 드라이브 설계",
}
CONCEPT_CHAPTERS = (1, 2, 3)  # 설계 문제가 아니라 개념을 다루는 장

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
TEST_CHAPTER = os.environ.get("STUDY_TEST_CHAPTER", "").strip()  # 지정하면 해당 장 예습/복습을 기록 없이 전송

PROGRESS_FILE = "study_progress.txt"
PREVIEW_PREFIX = "sdi:preview:"
REVIEW_PREFIX = "sdi:review:"
MAX_DOC_CHARS = 40000  # 발표자료 1개당 Gemini에 넘길 최대 글자 수

PREVIEW_SECTIONS = [
    ("summary", "📘 챕터 핵심 정리", 0x2ECC71),
    ("reading_points", "👀 읽으면서 생각해볼 것", 0x1ABC9C),
]
REVIEW_SECTIONS = [
    ("recap", "🧭 핵심 리캡", 0x95A5A6),
    ("presenter_notes", "🔍 발표자별 보충 포인트", 0x9B59B6),
    ("study_qna", "❓ 스터디 질문 & 토론", 0xE67E22),
    ("ai_questions", "🎯 AI 예상 질문", 0x3498DB),
]


def _schema(sections):
    keys = [key for key, _, _ in sections]
    return {
        "type": "OBJECT",
        "properties": {key: {"type": "STRING"} for key in keys},
        "required": keys,
        "property_ordering": keys,
    }


def chapter_title(ch):
    return f"{ch}장. {CHAPTERS[ch]}"


def chapter_url(ch):
    return f"https://github.com/{STUDY_REPO}/tree/{STUDY_BRANCH}/{quote(f'{ch:02d}장')}"


# ==========================================
# 2. 데이터 수집 (발표자료 / 이슈)
# ==========================================
def _github_get(path, **params):
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    res = requests.get(f"https://api.github.com/repos/{STUDY_REPO}/{path}",
                       params=params, headers=headers, timeout=20)
    res.raise_for_status()
    return res.json()


def get_presentations(ch):
    """장 폴더의 발표자료(.md)를 [{presenter, body, images}]로 반환합니다. 상대 경로 이미지는 raw URL로 바꿉니다."""
    docs = []
    for item in _github_get(f"contents/{quote(f'{ch:02d}장')}", ref=STUDY_BRANCH):
        if item["type"] != "file" or not item["name"].endswith(".md"):
            continue
        res = requests.get(item["download_url"], headers=HTTP_HEADERS, timeout=20)
        res.raise_for_status()
        body = res.text
        base_url = item["download_url"].rsplit("/", 1)[0] + "/"

        images = []
        srcs = re.findall(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)", body) + re.findall(r'<img[^>]+src="([^"]+)"', body)
        for src in srcs:
            image_url = src if src.startswith("http") else urljoin(base_url, quote(src, safe="/%"))
            if image_url not in images:
                images.append(image_url)

        # 파일명 규칙: [X주차]_X장_제목_이름.md
        presenter = item["name"][:-3].rsplit("_", 1)[-1].strip()
        docs.append({"presenter": presenter, "body": body, "images": images})
    return docs


_issue_cache = []


def _all_issues():
    if not _issue_cache:
        page = 1
        while True:
            batch = _github_get("issues", state="all", per_page=100, page=page)
            _issue_cache.extend(x for x in batch if "pull_request" not in x)
            if len(batch) < 100:
                break
            page += 1
    return _issue_cache


def get_issues(ch):
    """제목의 'N장'(주차가 아닌 장 번호)이 ch인 이슈를 [{author, title, body, comments}]로 반환합니다."""
    issues = []
    for issue in sorted(_all_issues(), key=lambda x: x["number"]):
        title = issue["title"].strip()
        match = re.search(r"(\d+)(?:-\d+)?장", title)
        if "(Example)" in title or not match or int(match.group(1)) != ch:
            continue
        comments = _github_get(f"issues/{issue['number']}/comments") if issue["comments"] else []
        issues.append({
            "author": title.rsplit("_", 1)[-1].strip(),
            "title": title,
            "body": issue.get("body") or "",
            "comments": [c.get("body") or "" for c in comments],
        })
    return issues


def format_presentations(docs):
    return "\n\n".join(
        f"===== 발표자: {d['presenter']} =====\n{d['body'][:MAX_DOC_CHARS]}" for d in docs
    ) or "(발표자료 없음)"


def format_issues(issues):
    blocks = []
    for i in issues:
        comments = "\n---\n".join(i["comments"]) or "(댓글 없음)"
        blocks.append(f"===== 이슈 작성자: {i['author']} =====\n[본문]\n{i['body']}\n[댓글]\n{comments}")
    return "\n\n".join(blocks) or "(이 장에는 올라온 이슈가 없음)"


# ==========================================
# 3. Gemini 요약
# ==========================================
DESIGN_STRUCTURE = """
    - 책의 설계 흐름을 따라 아래 4단계 소제목으로 정리하세요.
      **1️⃣ 요구사항**: 기능/비기능 요구사항, 개략적 규모 추정 수치
      **2️⃣ 개략 설계**: 핵심 구성 요소와 요청/데이터 흐름
      **3️⃣ 상세 설계**: 핵심 기법·알고리즘을 `• 기법명 — 동작 / 장점 / 단점 / 언제 쓰나` 형태로 비교
      **4️⃣ 마무리**: 추가로 논의할 점 (장애 대응, 확장, 모니터링 등)
"""

CONCEPT_STRUCTURE = """
    - 설계 문제가 아니라 개념을 다루는 장이므로, 발표자료에 개념이 나오는 순서대로 소제목을 나눠 정리하세요.
"""

PREVIEW_FORMAT = """
    [작성 양식]
    ■ summary: 챕터 핵심 정리 (공백 포함 약 3500~4000자)
    - 첫 줄은 `**한 줄 요약**: ...` 으로 이 장에서 무엇을 왜 다루는지 쓰세요.
    {structure}
    - 각 섹션은 `**소제목**` 한 줄 뒤에 설명을 쓰세요. Discord는 표를 지원하지 않으니 표 대신 목록을 쓰세요.
    - 모든 발표자료의 내용을 합쳐 하나의 정리로 만드세요. 발표자 이름은 쓰지 마세요.
    - 구체적인 수치, 예시, 트레이드오프를 생략하지 마세요. 첨부 다이어그램이 있으면 반영하세요.
    - 발표자료가 짧아 빠진 부분이 있으면 이 장에서 다루는 표준적인 내용으로 보충하되, 확실한 내용만 쓰세요.
    - 마지막 줄은 `**🔑 키워드**: 키워드 · 키워드 · ...` 로 쓰세요.

    ■ reading_points: 읽으면서 생각해볼 것 (공백 포함 약 500~800자)
    - 책을 읽기 전에 품고 들어갈 질문 3개를 `**1.** 질문` 형식으로 쓰세요. 답은 쓰지 마세요.
"""

REVIEW_FORMAT = """
    [작성 양식]
    ■ recap: 핵심 리캡 (공백 포함 약 500~800자)
    - 지난주에 상세 정리를 이미 읽은 사람이 기억을 되살리는 용도이므로, 상세 설명은 반복하지 마세요.
    - 이 장의 핵심 설계 결정(또는 개념) 5~7개를 `- 결정 — 이유` 형식으로 한 줄씩 쓰세요.
      예) `- 카운터 저장소로 Redis를 쓴다 — INCR/EXPIRE로 원자적 증가와 만료를 쉽게 처리한다`
    - 마지막 줄은 `**🔑 키워드**: 키워드 · 키워드 · ...` 로 쓰세요.

    ■ presenter_notes: 발표자별 보충 포인트 (공백 포함 약 1500~3000자)
    - 여러 발표자가 공통으로 다룬 기본 내용은 쓰지 마세요.
    - 한 발표자만 다룬 내용(직접 작성한 코드, 실무 사례, 책 밖의 추가 조사, 독자적인 해석 등)만 골라 정리하세요.
    - 발표자마다 `**👤 이름**` 한 줄 뒤에 `- ` 목록으로 쓰세요. 고유한 내용이 없는 발표자는 생략하세요.
    - 발표자가 1명뿐이면, 그 발표자료에서 책 내용 외에 덧붙인 부분만 쓰세요. 그런 부분이 없으면 "이 장은 발표자료에 별도 보충 내용이 없다." 한 줄만 쓰세요.

    ■ study_qna: 스터디 질문 & 토론 (공백 포함 약 2500~3800자)
    - 아래 [스터디 이슈]에 있는 질문만 다루세요. 이슈에 없는 질문을 지어내지 마세요.
    - 비슷한 질문은 하나로 묶고, 질문마다 아래 형식으로 쓰세요. 질문이 많으면 각 답변을 짧게 줄이세요.

    **Q. 질문 요약** (질문자 이름)
    > 💬 스터디 토론: 댓글에서 나온 답/의견 요약  ← 댓글에 답이 있을 때
    > 🤖 AI 답변: ||답변||  ← 댓글이 없거나 댓글에 답이 없을 때 (줄바꿈 없이 `||`로 감싸기)

    - 질문이 아니라 중요한 부분을 정리한 이슈는 `**📌 스터디원 포인트** (이름)` 뒤에 한두 줄로 요약하세요.
    - 이슈가 하나도 없으면 "이 장에는 올라온 질문 이슈가 없다." 한 줄만 쓰세요.

    ■ ai_questions: AI 예상 질문 (공백 포함 약 2500~3500자)
    - 이 장 내용을 바탕으로 한 시스템 설계 면접 예상 질문 3개. 스터디 이슈 질문과 겹치지 않게 하세요.
    - 힌트는 정답을 직접 말하지 않는 핵심 키워드 2~4개를 ` · `로 구분해 한 줄로 쓰세요.
    - 모범 답안은 Discord 스포일러로 가리기 위해 `||`로 감싸고, 답안 안에서는 줄바꿈하지 마세요.
    - `1.` 같은 번호 목록은 쓰지 말고 반드시 아래 형식을 따르세요.

    **Q1. 질문 내용**
    > 💡 키워드 · 키워드 · 키워드
    > ✅ ||모범 답안 내용||

    (Q2, Q3도 같은 형식)

    - 마지막에 `**✅ 셀프 체크**` 한 줄 뒤에, 이 장을 이해했는지 스스로 확인할 수 있는 체크 항목 3개를 `- [ ] ...` 형식으로 쓰세요.
"""

STUDY_ROLE = """
    당신은 『가상 면접 사례로 배우는 대규모 시스템 설계 기초』 스터디를 이끄는 시니어 백엔드 엔지니어이자 기술 면접관입니다.
    아래는 스터디원들이 이 책의 {title}을 읽고 작성한 발표자료입니다. 발표자료에 근거해 한국어로 작성하세요.
"""


def summarize_preview(ch, docs):
    structure = CONCEPT_STRUCTURE if ch in CONCEPT_CHAPTERS else DESIGN_STRUCTURE
    prompt = f"""
    {STUDY_ROLE.format(title=chapter_title(ch))}
    {SUMMARY_GUIDE}
    [발표자료]
    {format_presentations(docs)}
    {PREVIEW_FORMAT.format(structure=structure)}
    """
    schema = _schema(PREVIEW_SECTIONS)
    images = [u for d in docs for u in d["images"]]
    if not USE_GEMINI_IMAGES:
        return _generate(prompt, schema)
    return _generate([prompt, *download_image_parts(images)], schema)


def summarize_review(ch, docs, issues):
    prompt = f"""
    {STUDY_ROLE.format(title=chapter_title(ch))}
    스터디원들이 이 장에 대해 GitHub 이슈로 남긴 질문과 댓글도 함께 제공합니다.
    {SUMMARY_GUIDE}
    [발표자료]
    {format_presentations(docs)}
    [스터디 이슈]
    {format_issues(issues)}
    {REVIEW_FORMAT}
    """
    return _generate(prompt, _schema(REVIEW_SECTIONS))


# ==========================================
# 4. 메인 실행 로직
# ==========================================
def send_preview(ch):
    docs = get_presentations(ch)
    print(f"[예습] {chapter_title(ch)}: 발표자료 {len(docs)}개 ({', '.join(d['presenter'] for d in docs)})")
    report = summarize_preview(ch, docs)
    images = [u for d in docs for u in d["images"]]
    return send_discord_report("📖 [예습]", chapter_title(ch), chapter_url(ch), report, images, PREVIEW_SECTIONS)


def send_review(ch):
    docs = get_presentations(ch)
    issues = get_issues(ch)
    print(f"[복습] {chapter_title(ch)}: 발표자료 {len(docs)}개, 이슈 {len(issues)}개 "
          f"({', '.join(i['author'] for i in issues)})")
    report = summarize_review(ch, docs, issues)
    return send_discord_report("🔁 [복습]", chapter_title(ch), chapter_url(ch), report, (), REVIEW_SECTIONS)


def load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def record_progress(item_id):
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")


def main():
    if TEST_CHAPTER:
        ch = int(TEST_CHAPTER)
        print(f"테스트 모드: {chapter_title(ch)} 예습/복습을 기록 없이 전송합니다.")
        ok = send_review(ch)
        time.sleep(1)
        ok = send_preview(ch) and ok
        sys.exit(0 if ok else 1)

    progress = load_progress()
    # 지난주에 예습한 장을 복습하고, 아직 예습하지 않은 다음 장을 예습
    review_ch = next((ch for ch in CHAPTERS
                      if PREVIEW_PREFIX + str(ch) in progress and REVIEW_PREFIX + str(ch) not in progress), None)
    preview_ch = next((ch for ch in CHAPTERS if PREVIEW_PREFIX + str(ch) not in progress), None)

    if review_ch is None and preview_ch is None:
        print("모든 장의 예습/복습을 보냈습니다. 스터디 완료!")
        return

    failed = False
    for prefix, ch, send in ((REVIEW_PREFIX, review_ch, send_review), (PREVIEW_PREFIX, preview_ch, send_preview)):
        if ch is None:
            continue
        try:
            if not send(ch):
                raise RuntimeError("디스코드 전송 실패")
            record_progress(prefix + str(ch))
            print(f"전송 완료: {prefix}{ch}")
        except Exception as e:
            print(f"{prefix}{ch} 처리 중 에러 발생: {e}")
            failed = True
        time.sleep(1)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
