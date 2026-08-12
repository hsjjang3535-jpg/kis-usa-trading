# kis-us-trading-bot

한국투자증권 **미국주식 전용** 봇 (국내 `kis-trading-bot`과 **별도** Railway 서비스).

## 현재 기능

- KIS 해외주식 시세/일봉 조회
- 미국 정규장(ET 09:30~16:00, 서머타임 자동) 감지
- **시뮬 + 방식A 실전(옵션)**: 같은 점검에서 조건 통과 중 **신호점수 최고 1종**만 실주문, 나머지는 **병렬 시뮬** / 익절 +5% · 손절 −2%
- **워치 스킵 요약**: 미진입 사유를 주기적으로 텔레그램 알림
- **중간·마감 보고**: KST 01:00 중간 보고, NY 정규장 종료 시 최종 손익 보고 (국내 15:10 보고와 유사)
- **동적·유동 워치**: 급증/상승/거래량 → (비면) 거래대금/체결강도/신고가 → 점수 통과분만 최대 10종 (고정 10개 채움 아님)
- `/health` HTTP (Railway)
- 텔레그램 시작/스크리너/체결/하트비트 알림
- `ENABLE_US_LIVE_ORDERS=false` (켤 때만 실주문)

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

- 프리장/애프터장
- 종목별 규칙·백테스트
