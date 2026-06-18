# SSen 대시보드 디자인 시스템

`dashboard/index.html`의 모든 UI 작업은 이 토큰과 규칙을 따른다. 임의 색상 하드코딩 금지 —
CSS 변수만 사용. 기준 구현은 index.html의 `:root` 블록(이 문서와 1:1 동기 유지).

## 색상 토큰

| 변수 | 값 | 용도 |
|---|---|---|
| `--bg-base` | #04111f | 페이지 배경 (딥 네이비) |
| `--bg-panel` | #071a2e | 섹션/패널 |
| `--bg-card` | #0a2240 | 카드 (hover: `--bg-card-hover` #0d2b50) |
| `--bg-input` | #061526 | 입력 필드, 차트 캔버스 배경 |
| `--accent1` | #3a9fd8 | 주 액센트(블루) — 탭, 버튼, 포커스, 글로우 |
| `--accent2` | #e05e30 | 보조 액센트(오렌지) |
| `--accent-green` | #1fb87a | 성공/신선도 |
| `--gold` | #e8b84b | 중립 경고 |
| `--up` / `--dn` / `--neu` | #e05e30 / #3a9fd8 / #6b8499 | **한국 관례: 상승=주황, 하락=청** |
| `--border` | rgba(58,159,216,.18) | 내부 구분선(테이블 행, explain-row 등) — 실선 |
| `--border-bright` | rgba(58,159,216,.45) | **패널 외곽선 — `1px dashed`** (header, controls, section, chart-box, card, explain-block) |
| `--text-primary/secondary/muted` | #ddeeff / #7fa8cc / #3d6080 | 텍스트 3단계 |
| `--glow1` / `--glow2` | rgba(58,159,216,.25) / rgba(224,94,48,.25) | 박스섀도 글로우 |
| `--radius-sm/md/lg` | 6 / 10 / 16px | 입력·뱃지 / 카드 / 섹션 |

차트 데이터 팔레트(테마 색 고정 할당, PALETTE 상수):
`#3a9fd8 #e05e30 #1fb87a #9b6bd6 #e8b84b #d44e7a #6ab04c #4c6bc4 #16a085 #c0392b #2980b9 #27ae60 #8e44ad #f39c12 #e74c3c #1abc9c #d35400 #7fa8cc`

테마 추이(`initThemeTrend`/`trendColorOf`)는 선택 순서(슬롯) 기준 고정 5색
(`#1fb87a #3a9fd8 #e05e30 #9b6bd6 #e8b84b`)을 우선 사용해 동시에 표시되는
5개 테마가 항상 서로 뚜렷하게 구분되게 하고, 6개 이상 동시 비교가 필요해지면
나머지 PALETTE 색상으로 확장한다.

## 타이포그래피

- 폰트: `'Pretendard','Apple SD Gothic Neo','Noto Sans KR',sans-serif`
  (CDN: jsdelivr `orioncactus/pretendard@v1.3.9` static css). Chart.js에도 전역 적용:
  `Chart.defaults.font.family`, `Chart.defaults.color = '#7fa8cc'`
- 본문 14px / 카드 값 22px·700·letter-spacing -.02em / 테이블 헤더 10.5px 대문자 letter-spacing .6px
- 페이지 타이틀 1.15rem·700, 좌측에 펄스 닷(시그니처 요소)

## 시그니처 패턴

1. **펄스 닷**: 헤더 타이틀 좌측 10px 원, `--accent1` + `box-shadow: 0 0 8px`, 2s opacity 펄스.
   `prefers-reduced-motion`이면 애니메이션 제거
2. **글로우 칩**: 테마 선택 칩 — 비선택은 보더만, 선택 시 테마색 배경 + `0 0 8px {색}66` 글로우.
   border-radius 20px, 최대 5개 선택, 최소 1개 유지
3. **힌트 바**(.sec-hint): 좌측 2px `--accent1` 보더 + 옅은 블루 배경 — 차트 읽는 법 안내

## 차트 공통 규칙 (Chart.js)

- 그리드: `rgba(58,159,216,.06)` / 틱: `#3d6080`
- 툴팁: bg `#071a2e`, border `rgba(58,159,216,.35)`, title `#ddeeff`, body `#7fa8cc`, padding 10
- 라인: borderWidth 2.5, tension .35, pointBorderColor `#04111f`, 데이터 40포인트 초과 시 pointRadius 0
- 등락 표기: 항상 부호 포함 `+x.x%`
- 트리맵 4색 이산 팔레트 (정규화 t를 4분위 구간으로 매핑, 값이 클수록 진하게):
  상승 `#ff8c00 → #ff5833 → #c70038 → #920c3f` (pizazz→rose bud cherry)
  하락 `#6fb4e2 → #4a7cba → #2e4a9e → #1e2876` (viking→lucky point)
- 강세분포 스택(트리맵 4색과 동일 계열): 주도 `#920c3f` / 강세 `#ff5833` / 중립 `#6fb4e2` / 약세 `#1e2876`

## 품질 바닥선

반응형(900px에서 그리드 1열), focus 시 `--accent1` 보더 + glow, reduced-motion 대응,
input[type=date]에 `color-scheme: dark`.
