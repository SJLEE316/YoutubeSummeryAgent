import os
import requests
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai

# ==========================================
# 1. 설정 및 환경 변수 로드
# ==========================================
CHANNEL_IDS = [
    "UCZgt6AzoyjslHTC9dz0UoTw",  # 실제 구독 채널 ID로 변경
]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
KAKAO_REFRESH_TOKEN = os.environ.get("KAKAO_REFRESH_TOKEN")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


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
    # 카카오톡 메시지 본문 제한(약 1000자 안전 기준) 고려
    # -------------------------------------------------------------
    max_title_len = 50
    trimmed_title = title if len(title) <= max_title_len else title[:max_title_len] + "..."

    # 기본 헤더/링크 구문 작성
    header_text = f"🎬 [유튜브 영상 요약]\n\n📌 제목: {trimmed_title}\n🔗 링크: {url}\n\n📝 AI 요약 내용:\n"
    
    # 헤더 길이 포함 전체 950자 이내로 요약문 자르기
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
        api_url, headers=headers, data={"template_object": str(template_object).replace("'", '"')}
    )

    if res.status_code == 200 and res.json().get("result_code") == 0:
        print("카카오톡 메시지 전송 성공!")
        return True
    else:
        print(f"카카오톡 전송 실패: {res.status_code}, {res.text}")
        return False


# ==========================================
# 3. Gemini 요약 함수
# ==========================================
def summarize_with_gemini(transcript_text, title):
    """Gemini API를 활용하여 자막 요약, 추가 지식, 기술 면접 질문을 생성합니다."""
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = f"""
    당신은 IT/기술 분야의 전문 컨설턴트이자 면접관입니다.
    아래 유튜브 영상 "{title}"의 자막을 분석하여 정리해 주세요.
    전체 응답 길이는 공백 포함 **600자 이내**로 짧고 명확하게 작성해 주세요.

    [작성 양식]
    🎬 **[영상 핵심 요약]**
    - 자막 기반 핵심 내용 2~3줄 요약

    💡 **[AI 추가 배경지식]**
    - 영상 외 주제/용어 이해를 돕는 추가 배경 지식 1~2줄

    🎯 **[기술 면접 예상 질문]**
    - 이 기술 주제 관련 예상 면접 질문 1개와 1줄 힌트

    [자막 내용]
    {transcript_text[:10000]}
    """

    response = model.generate_content(prompt)
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
            ytt_api = YouTubeTranscriptApi()
            try:
                transcript = ytt_api.fetch(video_id, languages=["ko"])
            except Exception:
                transcript = ytt_api.fetch(video_id, languages=["en"])

            transcript_text = (" ".join([item.get('text', '') for item in transcript.fetch() if 'text' in item]) 
              if hasattr(transcript, 'fetch') else " ".join([i['text'] for i in transcript]))

            summary = summarize_with_gemini(transcript_text, video_title)
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