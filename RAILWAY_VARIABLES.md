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
| `PORT` | `8080` | Railway 헬스체크 |

## 권장

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `ENABLE_US_SIM` | `true` | 미국 시뮬 ON |
| `ENABLE_US_LIVE_ORDERS` | `false` | 방식A: 세션당 첫 1종목만 실주문 |
| `US_LIVE_AMOUNT_USD` | `200` | 실전 1회 매수 예산(USD) |
| `US_DYNAMIC_WATCHLIST` | `true` | 동적 스크리너 (거래량급증+상승률 순위→TOP N) |
| `US_MAX_WATCHLIST` | `10` | 동적 후보 상위 N |
| `US_SCREEN_MIN_RATE` | `2.0` | 스크린 등락률 하한(%) |
| `US_SCREEN_INTERVAL_MIN` | `30` | 후보 재갱신 주기(분) |
| `US_EXCLUDE_MEGA_CAP` | `true` | 시총 상위·초대형 블록 제외 |
| `US_MEGA_CAP_RANK_CUTOFF` | `50` | 시총순위 이내 제외 |
| `US_FALLBACK_WATCHLIST` | `SOFI:NAS,...` | 순위 실패 시 중형 폴백 (메가캡 금지) |
| `US_SCREEN_MIN_RATE_RELAXED` | `0.5` | 풀 비면 등락 하한 완화 |
| `ENABLE_US_S_RULE` | `true` | S·RVOL 규칙 |
| `ENABLE_US_ORB` | `true` | ORB(오프닝 레인지 돌파) |
| `ENABLE_US_GAP_GO` | `true` | Gap & Go (시가갭+ORB고+RVOL) |
| `US_REQUIRE_ABOVE_VWAP` | `true` | 롱은 근사 VWAP 위만 |
| `US_GAP_MIN_PCT` | `2.5` | Gap&Go 최소 시가갭 % |
| `US_ORB_MINUTES` | `15` | ORB 레인지 분 |
| `US_SIM_MIN_RVOL` | `2.5` | S 시간보정 RVOL 하한 |
| `US_WATCHLIST` | `AAPL:NAS,...` | 동적 실패 시 폴백 / 고정 모드 |
| `US_POLL_INTERVAL_MIN` | `5` | 정규장 점검 주기(분) |
| `US_SIM_AMOUNT_USD` | `500` | 시뮬 1회 가상 매수(USD) |
| `US_SIM_STOP_LOSS_PCT` | `2.0` | 손절 % |
| `US_SIM_TAKE_PROFIT_PCT` | `5.0` | 익절 % (하드) |
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
