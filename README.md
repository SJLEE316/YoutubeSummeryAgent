# Tech Summary Agent

기술 블로그 글과 유튜브 영상을 Gemini로 요약해 **하루에 하나씩** Discord로 보내주는 GitHub Actions 봇입니다.

- 📰 **블로그**: [ByteByteGo 블로그](https://blog.bytebytego.com/archive)의 글 본문을 읽고 요약
- 🎬 **유튜브**: 구독 채널에 최근 올라온 영상을 요약 (일요일에만 확인)
- 🎯 요약마다 기술 배경지식과 **기술 면접 예상 질문 3개**를 함께 정리

## 동작 방식

매일 한국시간 오전 9시에 한 번 실행되며, 한 번에 **1개만** 전송합니다.

| 요일 | 동작 |
|---|---|
| 월~토 | 블로그 글 1개 전송 |
| 일요일 | 최근 7일 안에 올라온 안 보낸 영상이 있으면 **영상 1개만** 전송, 없으면 블로그 글 1개 전송 |

### 블로그 글 선택
1. Substack 아카이브 API로 글 목록을 최신순으로 가져옵니다 (페이지당 12개).
2. 아직 보내지 않은 **무료 글** 중 가장 최근 것을 고릅니다. 새 글이 없으면 과거 글을 최신순으로 하나씩 보냅니다.
3. 현재 페이지의 글을 모두 보냈을 때만 다음 페이지를 읽습니다 (최대 10페이지).
4. 유료 구독자 전용 글(`only_paid`)은 미리보기만 제공되므로 건너뜁니다.

### 영상 선택
1. YouTube Data API(`playlistItems`)로 채널의 최근 업로드 목록을 가져옵니다.
2. 최근 7일 안에 올라왔고 아직 보내지 않은 영상 중 가장 최근 것을 고릅니다.
3. 영상 제목과 설명란을 기반으로 요약합니다. (영상 자체를 시청·분석하지는 않습니다)

### 처리 기록
보낸 항목은 `processed_videos.txt`에 기록되고, 실행이 끝나면 Actions가 자동으로 커밋합니다.

```
Dddv7P_k60o                              # 유튜브 영상 ID
blog:llm-prompt-caching-versus-semantic  # 블로그 글 slug
```

이 파일에서 줄을 지우면 해당 항목을 다시 보낼 수 있습니다.

## 설정

### 1. Discord Webhook 만들기
Discord 채널 설정(⚙️) → **연동** → **웹후크** → **새 웹후크** → **웹후크 URL 복사**

### 2. GitHub Secrets 등록
저장소 **Settings → Secrets and variables → Actions**

| Secret | 설명 |
|---|---|
| `DISCORD_WEBHOOK_URL` | 요약을 받을 Discord 채널의 Webhook URL |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey)에서 발급한 Gemini API 키 |
| `YOUTUBE_API_KEY` | Google Cloud에서 발급한 YouTube Data API v3 키 |

> Webhook URL을 아는 사람은 누구나 해당 채널에 메시지를 보낼 수 있으니 코드나 `.env`에 넣어 커밋하지 마세요.

### 3. Workflow 활성화
**Actions → Daily YouTube Summarizer**가 비활성화되어 있다면 **Enable workflow**를 눌러 켭니다.

## 실행

### 자동 실행
- **스케줄**: 매일 UTC 00:00 (한국시간 09:00)
- **push**: `main` / `master` 브랜치에 push할 때마다 1회 실행 (`auto` 모드)

### 수동 실행
**Actions → Daily YouTube Summarizer → Run workflow**에서 실행 대상을 선택할 수 있습니다.

| `run_mode` | 동작 |
|---|---|
| `auto` (기본) | 일요일은 영상 우선, 그 외에는 블로그 |
| `blog` | 요일과 관계없이 블로그 글 1개 |
| `video` | 요일과 관계없이 최근 7일 이내 영상 1개 |

### 로컬 실행
```bash
pip install -r requirements.txt

export GEMINI_API_KEY=...
export YOUTUBE_API_KEY=...
export DISCORD_WEBHOOK_URL=...
export RUN_MODE=blog   # auto | blog | video

python main.py
```

## 커스터마이징

`main.py` 상단의 설정값을 수정합니다.

| 설정 | 기본값 | 설명 |
|---|---|---|
| `CHANNEL_IDS` | ByteByteGo 채널 | 확인할 유튜브 채널 ID 목록 (`UC...`) |
| `BLOG_URL` | `https://blog.bytebytego.com` | 요약할 Substack 블로그 주소 |
| `VIDEO_RECENT_DAYS` | `7` | 이 기간 안에 올라온 영상만 새 영상으로 간주 |
| `MAX_BLOG_PAGES` | `10` | 안 보낸 글을 찾기 위해 읽을 최대 페이지 수 |
| `GEMINI_MODEL` | `gemini-3.6-flash` | 요약에 사용할 Gemini 모델 |
| `GEMINI_MAX_RETRIES` | `3` | Gemini 호출 실패 시 재시도 횟수 |

요약 형식과 어조는 `SUMMARY_GUIDE`, `SUMMARY_FORMAT` 프롬프트에서 바꿀 수 있습니다.

Substack 기반 블로그라면 `BLOG_URL`만 바꿔서 다른 블로그에도 사용할 수 있습니다.

## 참고 사항

- 블로그 데이터는 Substack의 비공식 API(`/api/v1/archive`, `/api/v1/posts`)를 사용합니다. Substack 사이트 공통 구조라 비교적 안정적이지만, 변경되면 수정이 필요할 수 있습니다.
- Discord 메시지는 4000자 단위로 나눠 전송합니다.
- Gemini API 무료 등급은 입력 내용이 Google 서비스 개선에 사용될 수 있습니다.
