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
TARGET_UUID = os.environ.get("KAKAO_TARGET_UUID")
SEND_MODE = os.environ.get("KAKAO_SEND_MODE", "3")  # 설정이 없으면 기본값 "3" (모두 전송)

youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


# [테스트용 Mock 데이터]
MOCK_SUMMARY = """🎬 **[영상 핵심 요약]**
- OpenAI가 데이터 에이전트를 구축하며 적용한 핵심 Arch 패턴 및 학습 데이터 파이프라인 정리.
- 에이전트 성능 향상을 위한 데이터 정제 자동화 및 피드백 루프 구조 소개.

💡 **[AI 추가 배경지식]**
- Data Agent는 비구조화된 데이터를 LLM이 학습/추론하기 쉬운 형태로 자동 변환해 주는 AI 파이프라인 시스템입니다.

🎯 **[기술 면접 예상 질문]**
- Q. LLM 에이전트 설계 시 데이터 품질을 자동 검증하기 위한 평가 지표(Metrics)는 어떻게 구성해야 할까요?"""

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
    """카카오톡 채널 연동 메시지를 친구(TARGET_UUID)에게 전송합니다."""
    access_token = get_kakao_access_token()
    if not access_token:
        print("Access Token이 없어 메시지를 전송하지 못했습니다.")
        return False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

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

    success_results = []

    # 1. 나에게 전송 (SEND_MODE가 "1" 또는 "3"일 때)
    if SEND_MODE in ["1", "3"]:
        me_url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
        me_payload = {"template_object": json.dumps(template_object, ensure_ascii=False)}
        res_me = requests.post(me_url, headers=headers, data=me_payload)
        
        if res_me.status_code == 200 and res_me.json().get("result_code") == 0:
            print("카카오톡 [나에게] 전송 성공!")
            success_results.append(True)
        else:
            print(f"카카오톡 [나에게] 전송 실패: {res_me.text}")
            success_results.append(False)

    
    
    # 2. 친구에게 전송 (SEND_MODE가 "2" 또는 "3"일 때)
    if SEND_MODE in ["2", "3"]:
        if not TARGET_UUID:
            print("KAKAO_TARGET_UUID가 설정되어 있지 않아 친구 전송을 건너땁니다.")
        else:
            friend_url = "https://kapi.kakao.com/v1/api/talk/friends/message/default/send"
            friend_payload = {
                "receiver_uuids": json.dumps([TARGET_UUID]),
                "template_object": json.dumps(template_object, ensure_ascii=False)
            }
            res_friend = requests.post(friend_url, headers=headers, data=friend_payload)
            
            if res_friend.status_code == 200 and res_friend.json().get("successful_receiver_uuids"):
                print("카카오톡 [친구에게] 전송 성공!")
                success_results.append(True)
            else:
                print(f"카카오톡 [친구에게] 전송 실패: {res_friend.text}")
                success_results.append(False)
                
    return any(success_results)


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
        model="gemini-2.0-flash-lite",
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
        # 1. UC 접두사를 UU로 바꿔 업로드 재생목록 ID 생성
        clean_id = channel_id.strip()
        uploads_playlist_id = "UU" + clean_id[2:] if clean_id.startswith("UC") else clean_id
        
        target_item = None
        next_page_token = None

        # 미처리 영상을 찾을 때까지 (또는 영상이 끝날 때까지) 페이지를 넘기며 탐색
        while True:
            try:
                playlist_response = youtube.playlistItems().list(
                    playlistId=uploads_playlist_id,
                    part="snippet",
                    maxResults=5,
                    pageToken=next_page_token
                ).execute()

                items = playlist_response.get("items", [])
                if not items:
                    break

                # 가져온 5개 중 미처리 영상 탐색
                for item in items:
                    v_id = item["snippet"]["resourceId"]["videoId"]
                    if v_id not in processed_videos:
                        target_item = item
                        break  # 찾았으므로 루프 탈출

                # target_item을 찾았거나, 더 이상 다음 페이지가 없으면 탐색 종료
                next_page_token = playlist_response.get("nextPageToken")
                if target_item or not next_page_token:
                    break

            except Exception as e:
                print(f"⚠️ 채널({channel_id}) API 요청 중 오류: {e}")
                break

        if not target_item:
            print(f"채널({channel_id}): 모든 영상이 이미 처리되었습니다.")
            continue

        video_id = target_item["snippet"]["resourceId"]["videoId"]
        video_title = target_item["snippet"]["title"]
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        print(f"요약 대상 영상 발견: '{video_title}' ({video_id})")
        
        try:
            # YouTube Data API v3로 영상 데이터 추출
            video_info = get_video_details_from_youtube_api(video_id)
            description = video_info["description"] if video_info else target_entry.get("summary", "")

            # Gemini 2.0-flash-lite 활용 요약문 생성
            # summary = summarize_with_gemini(video_title, video_url, description)
            summary = MOCK_SUMMARY  # 테스트용 Mock 데이터 사용
            
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