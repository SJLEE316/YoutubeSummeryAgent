import os
import re
import html
import time
from datetime import datetime, timedelta, timezone
import requests
from google import genai
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
KST = timezone(timedelta(hours=9))

PROCESSED_FILE = "processed_videos.txt"
BLOG_PREFIX = "blog:"  # 블로그 글은 영상 ID와 구분하기 위해 접두어를 붙여 기록
BLOG_PAGE_SIZE = 12
MAX_BLOG_PAGES = 10  # 안 보낸 글을 찾기 위해 읽을 최대 페이지 수
VIDEO_RECENT_DAYS = 7  # 이 기간 안에 올라온 영상만 '새 영상'으로 보고 전송
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_MAX_RETRIES = 3

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ==========================================
# 2. 디스코드 메시지 전송
# ==========================================
def send_discord_message(label, title, url, summary):
    """Discord Webhook으로 요약을 전송합니다. 길면 여러 메시지로 나눠 보냅니다."""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL이 설정되지 않아 메시지를 전송하지 못했습니다.")
        return False

    # Embed description 최대 4096자
    chunks = [summary[i:i + 4000] for i in range(0, len(summary), 4000)] or [""]

    for idx, chunk in enumerate(chunks):
        embed = {"description": chunk, "color": 0xFF0000}
        if idx == 0:
            embed["title"] = f"{label} {title}"[:256]
            embed["url"] = url

        res = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})
        if res.status_code not in (200, 204):
            print(f"디스코드 전송 실패: {res.status_code}, {res.text}")
            return False

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


def get_blog_body(slug):
    """Substack 글 API로 본문 HTML을 가져와 텍스트로 변환합니다."""
    res = requests.get(f"{BLOG_URL}/api/v1/posts/{slug}", headers=HTTP_HEADERS, timeout=20)
    res.raise_for_status()
    return html_to_text(res.json().get("body_html") or "")


# ==========================================
# 4. Gemini 요약 함수
# ==========================================
SUMMARY_GUIDE = """
    [작성 가이드]
    1. 추상적인 개념 설명은 지양하고, **기술적 메커니즘, 동작 원리, 사용된 기술 스택 및 키워드** 위주로 구체적으로 작성하세요.
    2. 모든 문장은 **`~한다`, `~이다` 형태의 평서체/단정형 어조**로 작성하세요. (예: ~합니다 -> ~한다 / ~입니다 -> ~이다)
    3. 전체 응답 길이는 공백 포함 **2000자 이내**로 작성하세요.
"""

SUMMARY_FORMAT = """
    [작성 양식]
    📝 [핵심 요약]
    - 주요 기술의 작동 메커니즘, 취약점/특징, 핵심 프로세스를 기술 용어를 사용하여 4~5줄 요약

    💡 [AI 추가 배경지식]
    - 기술 요소 원리, 구조적 한계/특징, 관련 아키텍처 등 심도 있는 기술 배경지식 3줄 정리

    🎯 [기술 면접 예상 질문]
    - 실무/아키텍처 수준의 기술 면접 질문 3개와 각각에 대한 구체적인 기술적 힌트(3줄 내외)
"""


def _generate(prompt):
    if not client:
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

    for attempt in range(GEMINI_MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt
            )
            return response.text.strip()
        except Exception as e:
            if attempt < GEMINI_MAX_RETRIES - 1:
                print(f"Gemini 요약 실패: {e}. {attempt + 1}번째 재시도 중... (3초 대기)")
                time.sleep(3)
            else:
                print("Gemini 최종 요약 실패 (최대 재시도 횟수 초과)")
                raise


def summarize_video_with_gemini(video_title, video_url, video_description):
    """Gemini API를 활용하여 영상 요약, 추가 지식, 기술 면접 질문을 생성합니다."""
    prompt = f"""
    당신은 IT/기술 분야의 최고 기술 책임자(CTO) 수준의 전문 컨설턴트이자 기술 면접관입니다.
    아래 유튜브 기술 영상의 정보와 링크 내용을 바탕으로 기술적 깊이가 있는 핵심 분석 보고서를 작성해 주세요.
    {SUMMARY_GUIDE}
    [영상 정보]
    - 제목: {video_title}
    - URL: {video_url}
    - 영상 설명란 내용:
    {video_description[:3000]}
    {SUMMARY_FORMAT}
    """
    return _generate(prompt)


def summarize_blog_with_gemini(title, body):
    """Gemini API를 활용하여 블로그 글 본문을 요약합니다."""
    prompt = f"""
    당신은 IT/기술 분야의 최고 기술 책임자(CTO) 수준의 전문 컨설턴트이자 기술 면접관입니다.
    아래 기술 블로그 글의 본문을 바탕으로 기술적 깊이가 있는 핵심 분석 보고서를 한국어로 작성해 주세요.
    본문 중 (Sponsored) 광고 문단은 무시해 주세요.
    {SUMMARY_GUIDE}
    [글 정보]
    - 제목: {title}
    - 본문:
    {body[:30000]}
    {SUMMARY_FORMAT}
    """
    return _generate(prompt)


# ==========================================
# 5. 메인 실행 로직
# ==========================================
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
        print(f"⚠️ 채널({channel_id}) API 요청 중 오류: {e}")
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
        summary = summarize_video_with_gemini(video_title, video_url, description)

        if send_discord_message("🎬", video_title, video_url, summary):
            record_processed(video_id, processed)
            print(f"성공적으로 처리 완료되어 기록되었습니다: {video_id}")
            return True
    except Exception as e:
        print(f"영상({video_id}) 처리 중 에러 발생: {e}")
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
        print(f"블로그 글 목록 수집 중 에러 발생: {e}")

    if not target_post:
        print("블로그: 보낼 글이 없습니다.")
        return False

    slug = target_post["slug"]
    title = target_post["title"]
    print(f"요약 대상 블로그 글 발견: '{title}' ({target_post['post_date'][:10]})")

    try:
        body = get_blog_body(slug)
        summary = summarize_blog_with_gemini(title, body)

        if send_discord_message("📰", title, target_post["canonical_url"], summary):
            record_processed(BLOG_PREFIX + slug, processed)
            print(f"성공적으로 처리 완료되어 기록되었습니다: {slug}")
            return True
    except Exception as e:
        print(f"블로그 글({slug}) 처리 중 에러 발생: {e}")
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

    if not has_processed:
        print("새로 요약해서 보낼 블로그 글/영상이 없거나 처리에 실패했습니다.")


if __name__ == "__main__":
    main()
