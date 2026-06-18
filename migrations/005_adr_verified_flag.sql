-- E9: ADR(상승하락비율) 확정 시점 검증 전 임시값 플래그.
-- 키움 OPT20009는 "오늘 현재" 스냅샷만 제공하며, 15:35/16:30 확정 시점 검증
-- (docs/kiwoom_collection_spec.md "ADR 확정 시점 검증") 결과가 나오기 전까지는
-- 키움으로 수집한 당일 ADR이 최종 확정치인지 불확실하다. 이를 행 단위로 표시.
--   true  : data.go.kr(공식 확정치) 또는 검증 완료 후 키움 수집
--   false : 검증 전 키움 당일 수집(잠정값) — 검증 결과에 따라 재확인/덮어쓰기 대상
ALTER TABLE fact_adr ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT true;
