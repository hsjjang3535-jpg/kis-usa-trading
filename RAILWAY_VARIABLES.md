# Railway Variables — 미국주식 봇

국내 `kis-trading-bot`과 **별도 서비스**에 설정합니다.

## 필수

| Variable | 예시 | 설명 |
|----------|------|------|
| `KIS_MODE` | `실전` | 모의/실전 |
| `KIS_APP_KEY` | (국내와 동일 가능) | 앱키 |
| `KIS_APP_SECRET` | | 시크릿 |
| `KIS_ACCOUNT_NO` | `12345678-01` | 통합증거금 계좌 |
| `TELEGRAM_BOT_TOKEN` | | 봇 토큰 |
| `TELEGRAM_CHAT_ID` | | 채팅 ID |
| `PORT` | `8080` | Railway 헬스체크 (`/healthz`) |

## 권장

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `ENABLE_US_SIM` | `true` | 미국 시뮬 ON |
| `ENABLE_US_LIVE_ORDERS` | `false` | 방식A: 같은 점검 점수순 실주문(세션 `US_LIVE_MAX_POSITIONS`종) |
| `US_LIVE_MAX_POSITIONS` | `3` | 세션당 실전 진입 최대 횟수. 동시 보유는 `US_MAX_TOTAL_USD`로 ≈2종 |
| `US_LIVE_AMOUNT_USD` | `750` (미설정 시 시뮬과 동일) | 실전 1회 매수 예산(USD, ≈100만원) |
| `US_MAX_TOTAL_USD` | `1500` | 실전 총 투자 한도(USD, ≈200만원) |
| `US_DYNAMIC_WATCHLIST` | `true` | 동적 스크리너 |
| `US_MAX_WATCHLIST` | `10` | 유동 후보 상한 (점수 통과분만, 억지 채움 없음) |
| `US_SCREEN_MIN_SCORE` | `2.0` | 유동 TOP 점수 하한 |
| `US_SCREEN_MIN_RATE` | `2.0` | 스크린 등락률 하한(%) |
| `US_SCREEN_INTERVAL_MIN` | `30` | 후보 재갱신 주기(분) |
| `US_SCREEN_VOL_RANG` | `0` | 순위 거래량 조건 (0=전체. 3이면 개장 직후 빈 풀이 잦음) |
| `US_SCREEN_INCLUDE_NYS` | `true` | 뉴욕 순위 합치기 |
| `US_EXCLUDE_MEGA_CAP` | `true` | 시총 상위·초대형 블록 제외 |
| `US_EXCLUDE_ETP` | `true` | 해외 ETP(ETF/ETN/레버리지 등) 제외 — 일반주만 |
| `US_MEGA_CAP_RANK_CUTOFF` | `50` | 시총순위 이내 제외 |
| `US_FALLBACK_WATCHLIST` | `SOFI:NAS,...` | 1·2차 순위 모두 실패 시에만 |
| `US_SCREEN_MIN_RATE_RELAXED` | `0.5` | 풀 비면 등락 하한 완화 |
| `ENABLE_US_S_RULE` | `true` | S·RVOL 규칙 |
| `ENABLE_US_ORB` | `true` | ORB(오프닝 레인지 돌파) |
| `ENABLE_US_GAP_GO` | `true` | Gap & Go (시가갭+ORB고+RVOL) |
| `US_REQUIRE_ABOVE_VWAP` | `true` | 롱은 근사 VWAP 위만 |
| `US_GAP_MIN_PCT` | `2.5` | Gap&Go 최소 시가갭 % |
| `US_ORB_MINUTES` | `15` | ORB 레인지 분 |
| `US_SIM_MIN_RVOL` | `2.5` | S 시간보정 RVOL 하한 |
| `US_WATCHLIST` | `AAPL:NAS,...` | 동적 실패 시 폴백 / 고정 모드 |
| `US_POLL_INTERVAL_MIN` | `5` | 정규장 점검 주기(분) — 신규 진입·스크리너 |
| `US_EXIT_POLL_SEC` | `60` | 보유 종목 손절·익절만 (초). 매수 후에만 동작 |
| `US_SIM_AMOUNT_USD` | `750` | 시뮬 1회 가상 매수(USD, ≈100만원) |
| `US_SIM_STOP_LOSS_PCT` | `2.0` | 손절 % |
| `US_SIM_TAKE_PROFIT_PCT` | `5.0` | 익절 % (하드) |
| `US_PARALLEL_SIM` | `true` | 주포지션 보유 중 다른 종목 병렬 시뮬 |
| `US_MAX_SIM_POSITIONS` | `5` | 병렬 시뮬 최대 종목 수 |
| `US_SKIP_NOTIFY_INTERVAL_MIN` | `120` | 워치 스킵 사유 텔레그램 요약 주기(분). 개장 직후는 생략 |
| `US_MID_REPORT_HOUR_NY` | `12` | 정규장 중간 보고 (NY 시각 이후 세션 중 1회, 놓치면 재시도) |
| `US_LOOP_STALE_SEC` | `300` | 정규장 중 루프 5분 무응답 시 헬스 503 → Railway 재시작 |
| `US_HEARTBEAT_HOUR_KST` | `22` | 일일 하트비트(KST) |
| `API_SECRET` | (임의) | `/health` 보호용 |

### 거래소 코드

- `NAS` 나스닥  
- `NYS` 뉴욕  
- `AMS` 아멕스  

## 체크리스트

- [ ] 국내 봇과 **다른** Railway 서비스/프로젝트
- [ ] `ENABLE_US_LIVE_ORDERS=false`
- [ ] 통합증거금 신청된 계좌
- [ ] 시작 텔레그램에 `🇺🇸 미국주식 봇 시작` 표시
