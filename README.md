# kis-us-trading-bot

한국투자증권 **미국주식 전용** 봇 (국내 `kis-trading-bot`과 **별도** Railway 서비스).

## 현재 기능

- KIS 해외주식 시세/일봉 조회
- 미국 정규장(ET 09:30~16:00, 서머타임 자동) 감지
- **시뮬만** 기본: **S(RVOL·시총필터)** + **ORB(15분 돌파)** 병행
- **동적 워치**: KIS 나스닥 거래량급증·상승률 **순위 API**에서 필터 후 TOP10 (티커 전수 스캔 아님, 메가캡 제외)
- `/health` HTTP (Railway)
- 텔레그램 시작/스크리너/체결/하트비트 알림
- `ENABLE_US_LIVE_ORDERS=false` (실주문 기본 OFF)

## Railway 배포

1. GitHub에 새 저장소 생성 후 push
2. Railway에서 **New Project → Deploy from GitHub** (국내 봇과 **다른 서비스**)
3. Variables 설정 (아래 `RAILWAY_VARIABLES.md`)
4. Deploy → 텔레그램 `🇺🇸 미국주식 봇 시작` 확인

국내 봇 Variables와 **같은** `KIS_APP_KEY` / `KIS_ACCOUNT_NO` / 텔레그램을 써도 됩니다.  
통합증거금 신청된 계좌를 그대로 사용합니다.

## 로컬 실행

```bash
cp .env.example .env
# .env 채우기
pip install -r requirements.txt
python trader.py
```

## 다음 단계 (원하면)

- 실전 매수/매도 연결 (`ENABLE_US_LIVE_ORDERS`)
- 프리장/애프터장
- 종목별 규칙·백테스트
