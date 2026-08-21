import os
import json
import requests
import feedparser
import time
from google import genai
from googleapiclient.discovery import build
from google.api_core.exceptions import ServiceUnavailable

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
KAKAO_CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET")
TARGET_UUID = os.environ.get("KAKAO_TARGET_UUID")
SEND_MODE = os.environ.get("KAKAO_SEND_MODE", "3")  # 설정이 없으면 기본값 "3" (모두 전송)

youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)



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
        "client_secret": KAKAO_CLIENT_SECRET.strip(),
    }
    response = requests.post(url, data=data)
    result = response.json()

    if "access_token" in result:
        return result["access_token"]
    else:
        print(f"카카오 Access Token 발급 실패: {result}")
        return None


# ==========================================
# 3. 카카오톡 메시지 전송 함수
# ==========================================
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

    # 1. Title 및 헤더 작성
    max_title_len = 50
    trimmed_title = title if len(title) <= max_title_len else title[:max_title_len] + "..."

    # 기본 헤더/링크 구문 작성
    header_text = f"🎬 [유튜브 영상 요약]\n\n📌 제목: {trimmed_title}\n🔗 링크: {url}"

    # 2. 구분자(---SPLIT---)를 기준으로 summary 분할
    if "---SPLIT---" in summary:
        parts = summary.split("---SPLIT---")
        part1 = parts[0].strip()
        part2 = parts[1].strip()
        messages = [
            f"{header_text}\n\n{part1}",
            f"{part2}"
        ]
    else:
        # 구분자가 없는 경우 기존처럼 1개의 메시지로 처리
        messages = [f"{header_text}📝 AI 요약 내용:\n{summary}"]

    overall_success = []

    for idx, message_text in enumerate(messages):
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
                print(f"카카오톡 [나에게] ({idx+1}/{len(messages)}) 전송 성공!")
                success_results.append(True)
            else:
                print(f"카카오톡 [나에게] ({idx+1}/{len(messages)}) 전송 실패: {res_me.text}")
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
                    print(f"카카오톡 [친구에게] ({idx+1}/{len(messages)}) 전송 성공!")
                    success_results.append(True)
                else:
                    print(f"카카오톡 [친구에게] ({idx+1}/{len(messages)}) 전송 실패: {res_friend.text}")
                    success_results.append(False)

        overall_success.append(any(success_results))

        # 순서 보장 및 도배 방지를 위한 미세 지연 (0.5초)
        if idx < len(messages) - 1:
            time.sleep(0.5)

    return all(overall_success)



# ==========================================
# 4. YouTube Data API v3를 활용한 영상 상세 정보 가져오기
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

# ==========================================
# 5. Gemini 요약 함수
# ==========================================
def summarize_with_gemini(video_title, video_url, video_description):
    """Gemini API를 활용하여 자막 요약, 추가 지식, 기술 면접 질문을 생성합니다."""
    # 1. Client 객체 생성 (API 키 전달)
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY")) if GEMINI_API_KEY else None
    if not client:
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

    prompt = f"""
    당신은 IT/기술 분야의 최고 기술 책임자(CTO) 수준의 전문 컨설턴트이자 기술 면접관입니다.
    아래 유튜브 기술 영상의 정보와 링크 내용을 바탕으로 기술적 깊이가 있는 핵심 분석 보고서를 작성해 주세요.

    [작성 가이드]
    1. 추상적인 개념 설명은 지양하고, **기술적 메커니즘, 동작 원리, 사용된 기술 스택 및 키워드** 위주로 구체적으로 작성하세요.
    2. 응답은 정확히 두 부분으로 나누어 작성하고, 두 부분 사이에 구분 기호 `---SPLIT---` 만 한 줄로 넣어주세요.
    3. 각 파트의 길이는 공백 포함 **800자~900자 이내**로 작성하세요.

    [영상 정보]
    - 제목: {video_title}
    - URL: {video_url}
    - 영상 설명란 내용:
    {video_description[:3000]}

    [작성 양식]
    🎬 [영상 핵심 요약]
    - 주요 기술의 작동 메커니즘, 취약점/특징, 핵심 프로세스를 기술 용어를 사용하여 4~5줄 요약

    💡 [AI 추가 배경지식]
    - 기술 요소 원리, 구조적 한계/특징, 관련 아키텍처 등 심도 있는 기술 배경지식 3줄 정리

    ---SPLIT---

    🎯 [기술 면접 예상 질문]
    - 실무/아키텍처 수준의 기술 면접 질문 3개와 각각에 대한 구체적인 기술적 힌트(3줄 내외)
    """


    # 2. 최신 SDK 방식의 컨텐츠 생성 호출
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
    )
    return response.text.strip()

# ==========================================
# 6. 메인 실행 로직
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
            description = video_info["description"] if video_info else target_item["snippet"].get("description", "")

            # Gemini 요약 (재시도 로직)
            summary = None
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    summary = summarize_with_gemini(video_title, video_url, description)
                    break  # 성공하면 루프 탈출
                except Exception as e:
                    if attempt < max_retries - 1:
                        print(f"Gemini 요약 실패: {e}. {attempt + 1}번째 재시도 중... (3초 대기)")
                        time.sleep(3)
                    else:
                        print(f"Gemini 최종 요약 실패 (최대 재시도 횟수 초과)")
                        raise e

            if not summary:
                continue
                
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