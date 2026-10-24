import os
import re
import sys
import html
import json
import time
from datetime import datetime, timedelta, timezone
import requests
from google import genai
from google.genai import types
from googleapiclient.discovery import build

# ==========================================
# 1. 설정 및 환경 변수 로드
# ==========================================
CHANNEL_IDS = [
    "UCZgt6AzoyjslHTC9dz0UoTw",  # 실제 구독 채널 ID로 변경
]
BLOG_URL = "https://blog.bytebytego.com"  # Substack 블로그 주소

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
RUN_MODE = os.environ.get("RUN_MODE", "auto")  # auto | blog | video
USE_GEMINI_IMAGES = os.environ.get("USE_GEMINI_IMAGES", "true").lower() != "false"
KST = timezone(timedelta(hours=9))

PROCESSED_FILE = "processed_videos.txt"
BLOG_PREFIX = "blog:"  # 블로그 글은 영상 ID와 구분하기 위해 접두어를 붙여 기록
BLOG_PAGE_SIZE = 12
MAX_BLOG_PAGES = 10  # 안 보낸 글을 찾기 위해 읽을 최대 페이지 수
VIDEO_RECENT_DAYS = 7  # 이 기간 안에 올라온 영상만 '새 영상'으로 보고 전송
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_RETRY_DELAYS = [30, 60, 120, 240]  # 재시도 전 대기 시간(초), 최초 시도 포함 최대 5번 시도
MAX_GEMINI_IMAGES = 5  # Gemini에 함께 넘길 본문 이미지 수
MAX_DISCORD_IMAGES = 4  # 상세 해설 아래에 붙일 이미지 수 (Discord 갤러리 최대 4장)
GEMINI_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/heic", "image/heif")

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ==========================================
# 2. 디스코드 메시지 전송
# ==========================================
REPORT_SECTIONS = [
    ("summary", "📝 상세 해설", 0xFF0000),
    ("background", "💡 AI 추가 배경지식", 0xF1C40F),
    ("interview", "🎯 기술 면접 예상 질문", 0x3498DB),
]


def _post_discord(embeds):
    res = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": embeds})
    if res.status_code not in (200, 204):
        print(f"디스코드 전송 실패: {res.status_code}, {res.text}")
        return False
    return True


def send_discord_report(label, title, url, report, image_urls=()):
    """
    요약 결과를 섹션별(상세 해설 / 배경지식 / 면접 질문)로 나눠 Discord Webhook으로 전송합니다.
    상세 해설 메시지 아래에는 이미지를 최대 MAX_DISCORD_IMAGES장 붙입니다.
    """
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL이 설정되지 않아 메시지를 전송하지 못했습니다.")
        return False

    for section_idx, (key, header, color) in enumerate(REPORT_SECTIONS):
        text = report.get(key, "").strip()
        # Embed description 최대 4096자
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]

        for chunk_idx, chunk in enumerate(chunks):
            embed = {"description": chunk, "color": color}
            if chunk_idx == 0:
                embed["title"] = (f"{label} {title}" if section_idx == 0 else header)[:256]
                embed["url"] = url
            if section_idx == 0 and chunk_idx == 0:
                embed["description"] = f"**{header}**\n\n{chunk}"[:4096]

            embeds = [embed]
            # 같은 url을 가진 embed는 Discord가 하나의 이미지 갤러리로 묶어서 보여줌
            if section_idx == 0 and chunk_idx == len(chunks) - 1 and image_urls:
                embeds[0] = {**embed, "url": url, "image": {"url": image_urls[0]}}
                embeds += [{"url": url, "image": {"url": u}} for u in image_urls[1:MAX_DISCORD_IMAGES]]

            if not _post_discord(embeds):
                return False
            time.sleep(0.5)  # 메시지 순서 보장

    print("디스코드 메시지 전송 성공!")
    return True


# ==========================================
# 3. 데이터 수집 (유튜브 영상 / 블로그 글)
# ==========================================
def get_video_details_from_youtube_api(video_id):
    """YouTube Data API v3를 활용하여 동영상 비디오 상세 정보를 가져옵니다."""
    if not YOUTUBE_API_KEY:
        return None
    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        request = youtube.videos().list(
            part="snippet,contentDetails",
            id=video_id
        )
        response = request.execute()
        items = response.get("items", [])
        if items:
            snippet = items[0]["snippet"]
            return {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channelTitle": snippet.get("channelTitle", "")
            }
    except Exception as e:
        print(f"YouTube Data API 호출 중 에러 발생: {e}")
    return None


def iter_blog_pages():
    """
    Substack 아카이브 API로 블로그 글 목록을 페이지 단위로 최신순으로 반환합니다.
    다음 페이지는 호출하는 쪽에서 요청할 때만 가져옵니다.
    """
    for page in range(MAX_BLOG_PAGES):
        res = requests.get(
            f"{BLOG_URL}/api/v1/archive",
            params={"sort": "new", "offset": page * BLOG_PAGE_SIZE, "limit": BLOG_PAGE_SIZE},
            headers=HTTP_HEADERS,
            timeout=20,
        )
        res.raise_for_status()
        posts = res.json()
        if not posts:
            return
        yield posts


def html_to_text(body_html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", body_html, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>|</(p|h[1-6]|li|blockquote|pre|figcaption)>", "\n", text)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def get_blog_content(slug):
    """
    Substack 글 API로 본문을 가져와 (텍스트, 이미지 URL 목록)으로 반환합니다.
    '(Sponsored)' 제목부터 다음 구분선(<hr>)까지의 광고 구간은 제외합니다.
    """
    res = requests.get(f"{BLOG_URL}/api/v1/posts/{slug}", headers=HTTP_HEADERS, timeout=20)
    res.raise_for_status()
    body_html = res.json().get("body_html") or ""

    sections = re.split(r"<hr[^>]*>", body_html)
    body_html = "".join(s for s in sections if "(Sponsored)" not in s)

    image_urls = []
    for src in re.findall(r'<img[^>]+src="([^"]+)"', body_html):
        src = html.unescape(src)
        if src not in image_urls:
            image_urls.append(src)

    return html_to_text(body_html), image_urls


def download_image_parts(image_urls):
    """이미지를 내려받아 Gemini 입력용 Part로 변환합니다. 지원하지 않는 형식(GIF 등)은 건너뜁니다."""
    parts = []
    for image_url in image_urls[:MAX_GEMINI_IMAGES]:
        try:
            res = requests.get(
                image_url,
                headers={**HTTP_HEADERS, "Accept": "image/jpeg,image/png,image/webp"},
                timeout=20,
            )
            res.raise_for_status()
            mime_type = res.headers.get("Content-Type", "").split(";")[0]
            if mime_type not in GEMINI_IMAGE_TYPES:
                print(f"Gemini 미지원 이미지 형식({mime_type})이라 건너뜁니다: {image_url}")
                continue
            parts.append(types.Part.from_bytes(data=res.content, mime_type=mime_type))
        except Exception as e:
            print(f"이미지 다운로드 실패({image_url}): {e}")
    return parts


# ==========================================
# 4. Gemini 요약 함수
# ==========================================
SUMMARY_GUIDE = """
    [작성 가이드]
    1. 추상적인 개념 설명은 지양하고, **기술적 메커니즘, 동작 원리, 사용된 기술 스택 및 키워드** 위주로 구체적으로 작성하세요.
    2. 모든 문장은 **`~한다`, `~이다` 형태의 평서체/단정형 어조**로 작성하세요. (예: ~합니다 -> ~한다 / ~입니다 -> ~이다)
    3. 응답은 JSON으로 하며, 각 필드 값에는 섹션 제목을 넣지 말고 본문만 Discord 마크다운으로 작성하세요.
"""

SUMMARY_FORMAT = """
    [작성 양식]
    ■ summary: 상세 해설 (공백 포함 약 3500자)
    - 원문을 읽지 않은 사람도 이해할 수 있도록, 원문의 소제목 흐름을 따라 섹션별로 해설하세요.
    - 각 섹션은 `**소제목**` 한 줄 뒤에 설명 문단을 쓰세요.
    - 처음 등장하는 기술 용어는 괄호나 짧은 문장으로 풀어 설명하세요.
    - 원문에 나온 구체적인 예시, 수치, 비교, 트레이드오프를 생략하지 말고 살리세요.
    - 첨부된 다이어그램 이미지가 있다면 그 내용도 해설에 반영하세요.

    ■ background: AI 추가 배경지식 (공백 포함 약 2000~2500자)
    - 원문을 이해하는 데 도움이 되는 기술 요소 원리, 구조적 한계/특징, 관련 아키텍처를 정리하세요.
    - 원문 내용을 반복하지 말고, 원문에 없지만 알아두면 좋은 내용 위주로 작성하세요.
    - 확실하지 않은 내용은 쓰지 마세요.

    ■ interview: 기술 면접 예상 질문 (공백 포함 약 3500자)
    - 실무/아키텍처 수준의 기술 면접 질문 3개와 각각에 대한 힌트, 모범 답안
    - 힌트는 정답을 직접 말하지 않고 답변 방향을 잡아주는 핵심 키워드 2~4개만 ` · `로 구분해 한 줄로 작성하세요.
    - 모범 답안은 면접에서 그대로 말할 수 있을 만큼 구체적으로 작성하세요.
    - 모범 답안은 Discord 스포일러로 가리기 위해 `||`로 감싸고, 답안 안에서는 줄바꿈하지 마세요.
    - `1.` 같은 번호 목록은 사용하지 말고, 반드시 아래 형식을 그대로 따르세요.

    **Q1. 질문 내용**
    > 💡 키워드 · 키워드 · 키워드
    > ✅ ||모범 답안 내용||

    **Q2. 질문 내용**
    > 💡 키워드 · 키워드 · 키워드
    > ✅ ||모범 답안 내용||

    **Q3. 질문 내용**
    > 💡 키워드 · 키워드 · 키워드
    > ✅ ||모범 답안 내용||
"""


REPORT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "background": {"type": "STRING"},
        "interview": {"type": "STRING"},
    },
    "required": ["summary", "background", "interview"],
    "property_ordering": ["summary", "background", "interview"],
}


def _log_token_usage(response):
    usage = response.usage_metadata
    if not usage:
        return
    print(
        "Gemini 토큰 사용량 - "
        f"입력: {usage.prompt_token_count}, "
        f"출력: {usage.candidates_token_count}, "
        f"사고: {getattr(usage, 'thoughts_token_count', None) or 0}, "
        f"합계: {usage.total_token_count}"
    )
    for detail in getattr(usage, "prompt_tokens_details", None) or []:
        print(f"  - 입력 {detail.modality}: {detail.token_count}")


def _generate(contents):
    """Gemini로 요약을 생성해 {summary, background, interview} dict로 반환합니다."""
    if not client:
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

    # 503(과부하) 같은 일시적 오류에 대비해 재시도 간격을 점점 늘림
    for attempt in range(len(GEMINI_RETRY_DELAYS) + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=REPORT_SCHEMA,
                ),
            )
            _log_token_usage(response)
            report = json.loads(response.text)
            for key, _, _ in REPORT_SECTIONS:
                print(f"  - {key}: {len(report.get(key, ''))}자")
            return report
        except Exception as e:
            if attempt < len(GEMINI_RETRY_DELAYS):
                delay = GEMINI_RETRY_DELAYS[attempt]
                print(f"Gemini 요약 실패: {e}. {delay}초 후 {attempt + 1}번째 재시도...")
                time.sleep(delay)
            else:
                print("Gemini 최종 요약 실패 (최대 재시도 횟수 초과)")
                raise


def summarize_video_with_gemini(video_title, video_url, video_description):
    """Gemini API를 활용하여 영상 요약, 추가 지식, 기술 면접 질문을 생성합니다."""
    prompt = f"""
    당신은 IT/기술 분야의 최고 기술 책임자(CTO) 수준의 전문 컨설턴트이자 기술 면접관입니다.
    아래 유튜브 기술 영상의 정보와 링크 내용을 바탕으로 기술적 깊이가 있는 핵심 분석 보고서를 작성해 주세요.
    영상 설명란에 근거가 없는 내용을 영상에 나온 것처럼 지어내지 마세요. 정보가 부족하면 summary는 짧아도 됩니다.
    {SUMMARY_GUIDE}
    [영상 정보]
    - 제목: {video_title}
    - URL: {video_url}
    - 영상 설명란 내용:
    {video_description[:3000]}
    {SUMMARY_FORMAT}
    """
    return _generate(prompt)


def summarize_blog_with_gemini(title, body, image_urls):
    """Gemini API를 활용하여 블로그 글 본문과 다이어그램 이미지를 요약합니다."""
    prompt = f"""
    당신은 IT/기술 분야의 최고 기술 책임자(CTO) 수준의 전문 컨설턴트이자 기술 면접관입니다.
    아래 기술 블로그 글의 본문과 첨부된 다이어그램 이미지를 바탕으로 기술적 깊이가 있는 분석 보고서를 한국어로 작성해 주세요.
    본문 중 (Sponsored) 광고 문단은 무시해 주세요.
    {SUMMARY_GUIDE}
    [글 정보]
    - 제목: {title}
    - 본문:
    {body[:30000]}
    {SUMMARY_FORMAT}
    """
    if not USE_GEMINI_IMAGES:
        print("이미지 사용 안 함 설정: 본문 텍스트만으로 요약합니다.")
        return _generate(prompt)
    return _generate([prompt, *download_image_parts(image_urls)])


# ==========================================
# 5. 메인 실행 로직
# ==========================================
errors = []  # 실행 중 발생한 오류 (하나라도 있으면 종료 코드 1로 Actions 실패 처리)


def report_error(message):
    print(message)
    errors.append(message)


def record_processed(item_id, processed):
    processed.add(item_id)
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")


def process_latest_video(channel_id, processed):
    """채널의 최근 VIDEO_RECENT_DAYS일 이내 미처리 영상 1개를 요약해 전송합니다."""
    # UC 접두사를 UU로 바꿔 업로드 재생목록 ID 생성 (최신 업로드 순)
    clean_id = channel_id.strip()
    uploads_playlist_id = "UU" + clean_id[2:] if clean_id.startswith("UC") else clean_id

    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        items = youtube.playlistItems().list(
            playlistId=uploads_playlist_id,
            part="snippet,contentDetails",
            maxResults=10,
        ).execute().get("items", [])
    except Exception as e:
        report_error(f"⚠️ 채널({channel_id}) API 요청 중 오류: {e}")
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(days=VIDEO_RECENT_DAYS)
    target_item = None
    for item in items:
        video_id = item["contentDetails"]["videoId"]
        published = item["contentDetails"].get("videoPublishedAt") or item["snippet"]["publishedAt"]
        published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if video_id not in processed and published_at >= cutoff:
            target_item = item
            break

    if not target_item:
        print(f"채널({channel_id}): 최근 {VIDEO_RECENT_DAYS}일 이내 새 영상이 없습니다.")
        return False

    video_id = target_item["contentDetails"]["videoId"]
    video_title = target_item["snippet"]["title"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"요약 대상 영상 발견: '{video_title}' ({video_id})")

    try:
        video_info = get_video_details_from_youtube_api(video_id)
        description = video_info["description"] if video_info else target_item["snippet"].get("description", "")
        report = summarize_video_with_gemini(video_title, video_url, description)

        thumbnails = target_item["snippet"].get("thumbnails", {})
        thumbnail = next((thumbnails[k]["url"] for k in ("maxres", "high", "medium") if k in thumbnails), None)

        if not send_discord_report("🎬", video_title, video_url, report, [thumbnail] if thumbnail else []):
            raise RuntimeError("디스코드 전송 실패")
        record_processed(video_id, processed)
        print(f"성공적으로 처리 완료되어 기록되었습니다: {video_id}")
        return True
    except Exception as e:
        report_error(f"영상({video_id}) 처리 중 에러 발생: {e}")
    return False


def process_latest_blog_post(processed):
    """블로그에서 아직 보내지 않은 무료 글 중 가장 최근 것 1개를 요약해 전송합니다."""
    # 새 글이 없으면 아직 보내지 않은 과거 글 중 가장 최근 것을 보냄
    # 현재 페이지에 안 보낸 글이 없을 때만 다음 페이지를 읽음
    # 유료 구독자 전용 글(only_paid)은 미리보기만 볼 수 있어 건너뜀
    target_post = None
    try:
        for page_num, posts in enumerate(iter_blog_pages(), start=1):
            target_post = next(
                (p for p in posts
                 if p.get("audience") == "everyone" and BLOG_PREFIX + p["slug"] not in processed),
                None,
            )
            if target_post:
                break
            print(f"블로그: {page_num}페이지의 글은 모두 처리됨, 다음 페이지 확인")
    except Exception as e:
        report_error(f"블로그 글 목록 수집 중 에러 발생: {e}")

    if not target_post:
        print("블로그: 보낼 글이 없습니다.")
        return False

    slug = target_post["slug"]
    title = target_post["title"]
    print(f"요약 대상 블로그 글 발견: '{title}' ({target_post['post_date'][:10]})")

    try:
        body, image_urls = get_blog_content(slug)
        print(f"본문 {len(body)}자, 이미지 {len(image_urls)}장")
        report = summarize_blog_with_gemini(title, body, image_urls)

        if not send_discord_report("📰", title, target_post["canonical_url"], report, image_urls):
            raise RuntimeError("디스코드 전송 실패")
        record_processed(BLOG_PREFIX + slug, processed)
        print(f"성공적으로 처리 완료되어 기록되었습니다: {slug}")
        return True
    except Exception as e:
        report_error(f"블로그 글({slug}) 처리 중 에러 발생: {e}")
    return False


def main():
    processed = set()
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            processed = set(line.strip() for line in f if line.strip())

    has_processed = False

    # auto: 일요일(한국시간)이면 새 영상을 먼저 확인하고, 영상을 보냈으면 블로그는 보내지 않음 (하루 1개)
    is_sunday = datetime.now(KST).weekday() == 6
    if RUN_MODE == "video" or (RUN_MODE == "auto" and is_sunday):
        for channel_id in CHANNEL_IDS:
            if process_latest_video(channel_id, processed):
                has_processed = True
                break

    if RUN_MODE == "blog" or (RUN_MODE == "auto" and not has_processed):
        has_processed = process_latest_blog_post(processed)

    if errors:
        print(f"오류 {len(errors)}건이 발생해 실패로 종료합니다.")
        sys.exit(1)
    if not has_processed:
        print("새로 요약해서 보낼 블로그 글/영상이 없습니다.")


if __name__ == "__main__":
    main()
