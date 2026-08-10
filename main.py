import os
import json
import requests
import feedparser
from google import genai
from googleapiclient.discovery import build

# ==========================================
# 1. 설정 및 환경 변수 로드
# ==========================================
CHANNEL_IDS = [
    "UCZgt6AzoyjslHTC9dz0UoTw",  # 실제 구독 채널 ID로 변경
]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
KAKAO_REFRESH_TOKEN = os.environ.get("KAKAO_REFRESH_TOKEN")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ==========================================
# 2. 카카오톡 액세스 토큰 갱신 및 메시지 전송
# ==========================================
def get_kakao_access_token():
    """Refresh Token을 이용해 새로운 Access Token을 발급받습니다."""
    url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_API_KEY,
        "refresh_token": KAKAO_REFRESH_TOKEN,
    }
    response = requests.post(url, data=data)
    result = response.json()

    if "access_token" in result:
        return result["access_token"]
    else:
        print(f"카카오 Access Token 발급 실패: {result}")
        return None


def send_kakao_message(title, url, summary):
    """카카오톡 '나에게 보내기'로 요약본을 전송합니다 (글자 수 세이프가드 적용)."""
    access_token = get_kakao_access_token()
    if not access_token:
        print("Access Token이 없어 메시지를 전송하지 못했습니다.")
        return False

    api_url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {access_token}"}

    # -------------------------------------------------------------
    # [글자 수 세이프가드 적용]
    # 카카오톡 메시지 본문 제한(약 1800자 안전 기준) 고려
    # -------------------------------------------------------------
    max_title_len = 50
    trimmed_title = title if len(title) <= max_title_len else title[:max_title_len] + "..."

    # 기본 헤더/링크 구문 작성
    header_text = f"🎬 [유튜브 영상 요약]\n\n📌 제목: {trimmed_title}\n🔗 링크: {url}\n\n📝 AI 요약 내용:\n"
    
    # 헤더 길이 포함 전체 1800자 이내로 요약문 자르기
    available_summary_len = 1800 - len(header_text)
    
    if len(summary) > available_summary_len:
        trimmed_summary = summary[:available_summary_len - 15] + "\n\n(내용이 길어 일부 생략됨)"
    else:
        trimmed_summary = summary

    message_text = header_text + trimmed_summary

    template_object = {
        "object_type": "text",
        "text": message_text,
        "link": {"web_url": url, "mobile_web_url": url},
        "button_title": "영상 보기",
    }

    res = requests.post(
    api_url, 
    headers=headers, 
    data={"template_object": json.dumps(template_object, ensure_ascii=False)}
)

    if res.status_code == 200 and res.json().get("result_code") == 0:
        print("카카오톡 메시지 전송 성공!")
        return True
    else:
        print(f"카카오톡 전송 실패: {res.status_code}, {res.text}")
        return False

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

# ==========================================
# 3. Gemini 요약 함수
# ==========================================
def summarize_with_gemini(video_title, video_url, video_description):
    """Gemini API를 활용하여 자막 요약, 추가 지식, 기술 면접 질문을 생성합니다."""
    if not client:
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

    prompt = f"""
    당신은 IT/기술 분야의 전문 컨설턴트이자 면접관입니다.
    아래 유튜브 기술 영상의 정보와 링크 내용을 기반으로 내용을 분석하고 핵심을 정리해 주세요.
    전체 응답 길이는 공백 포함 **800자 이내**로 작성해 주세요.

    [영상 정보]
    - 제목: {video_title}
    - URL: {video_url}
    - 영상 설명란 내용:
    {video_description[:3000]}

    [작성 양식]
    🎬 **[영상 핵심 요약]**
    - 영상의 핵심 주제 및 주요 내용 2~3줄 요약

    💡 **[AI 추가 배경지식]**
    - 영상 주제 및 용어 이해를 돕는 추가 기술 배경지식 1~2줄

    🎯 **[기술 면접 예상 질문]**
    - 이 기술 주제 관련 예상 면접 질문 1개와 1줄 힌트
    """

    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt
    )
    return response.text.strip()


# ==========================================
# 4. 메인 실행 로직
# ==========================================
def main():
    processed_videos = set()
    if os.path.exists("processed_videos.txt"):
        with open("processed_videos.txt", "r", encoding="utf-8") as f:
            processed_videos = set(line.strip() for line in f if line.strip())

    has_processed = False

    for channel_id in CHANNEL_IDS:
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        feed = feedparser.parse(rss_url)

        if not feed.entries:
            print(f"채널({channel_id})에서 영상 목록을 불러올 수 없습니다.")
            continue

        target_entry = None
        for entry in feed.entries:
            v_id = entry.yt_videoid
            if v_id not in processed_videos:
                target_entry = entry
                break

        if not target_entry:
            print(f"채널({channel_id}): 모든 영상이 이미 처리되었습니다.")
            continue

        video_id = target_entry.yt_videoid
        video_title = target_entry.title
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        print(f"요약 대상 영상 발견: '{video_title}' ({video_id})")

        try:
            # YouTube Data API v3로 영상 데이터 추출
            video_info = get_video_details_from_youtube_api(video_id)
            description = video_info["description"] if video_info else target_entry.get("summary", "")

            # Gemini 2.5 활용 요약문 생성
            summary = summarize_with_gemini(video_title, video_url, description)
            
            # 카카오톡 전송
            success = send_kakao_message(video_title, video_url, summary)

            if success:
                with open("processed_videos.txt", "a", encoding="utf-8") as f:
                    f.write(f"{video_id}\n")
                print(f"성공적으로 처리 완료되어 기록되었습니다: {video_id}")
                has_processed = True
                break

        except Exception as e:
            print(f"영상({video_id}) 처리 중 에러 발생: {e}")
            continue

    if not has_processed:
        print("새로 요약해서 보낼 영상이 없거나 처리에 실패했습니다.")


if __name__ == "__main__":
    main()