# Food Market Intelligence Platform: 요구사항 정리

출처: `docs/20261106_*.jpg` 4장 (2026 Q4 Food Market Intelligence Framework.xlsx 화면 촬영, 시트 2개: `mi framework`, `기존 데이터 정리`).
사진을 읽어 옮긴 것이라 오독 가능성이 있습니다. 확인이 필요한 항목은 "확인 필요"로 표시했습니다.

## 1. 개요

- 시장의 변화를 감지하고 의사결정을 지원하는 시스템.
- 여러 Signal(Search, Buyer, SKU, Promotion, News)을 결합해 시장 변화와 수요 이동을 자동 탐지하는 Framework.
- 현재는 Fresh를 대상으로 검증 중이며, 이후 다른 카테고리로 확장 가능해야 함.

## 2. 문제 정의

- 현재 MI는 Search Keyword 리포트, 뉴스 모니터링, 경쟁사 전단 및 프로모션 모니터링, 신규상품/트렌드 모니터링을 각각 따로 운영 중.
- 실제로 궁금한 것은 개별 데이터가 아니라 "무엇이 성장하고 있는가?", "무엇이 약화되고 있는가?", "고객 수요는 어디로 이동하고 있는가?".
- Search→GMV 분석 결과, 일부 카테고리에서는 Search가 실제 수요를 선행할 가능성이 있음. 다만 Search 데이터만으로 실제 수요를 설명하기에는 한계가 있음. 원인은 카테고리 매핑, Search와 구매 간 연결성, 카테고리 단위와 키워드 단위의 해석 차이 등 (사진 오른쪽이 잘려 있어 이후 문장은 확인 필요).
- 따라서 개별 운영 중인 Signal을 연결해 고객 관심, 실제 수요, 트렌드를 통합 해석하는 Framework가 필요함.

## 3. 제안 솔루션

Search, Product, Promotion, Media 데이터를 통합해 두 가지 관점으로 시장을 해석합니다.

| | Category Health Dashboard | Food Trend Radar |
|---|---|---|
| 목적 | 실제 수요 관점의 시장 진단 | 관심 변화 및 Emerging Signal 탐지 |
| 주요 질문 | 실제 수요가 증가/감소하고 있는가? 고객 수요는 어떤 영역으로 이동하고 있는가? 공급 대비 수요 상태는 어떤가? | 무엇이 새롭게 관심을 얻고 있는가? 어떤 트렌드가 형성되고 있는가? 향후 성장 가능성이 있는가? |
| 활용 데이터 | Buyer, Search, Search Buyer, SKU | Search, Product, Promotion, Media |
| 비고 | Buyer, Search, Search Buyer 간 관계는 추가 검증 필요 | 실제 수요를 측정하기보다 초기 관심 신호 탐지가 목적 |

**추가할 것**
- Trend Radar → Opportunity Radar 확장
- Trend Lifecycle (Seed / Emerging / Growing / Mainstream)
- Search → Demand Leading Indicator 검증 체계

**화면 구성**: What Changed? → Why Should I Care? → What Should I Do?

## 4. 기존 자산 / 진행 현황

| Signal | 데이터 | 현재 상태 |
|---|---|---|
| Search | 쿠팡 검색 | 정기 리포트 운영 중 |
| Product | 컬리 신규상품 | 크롤링 툴 구축 완료, 자동화 검토 중 |
| Promotion | 경쟁사 전단 | OCR 기반 데이터 추출 자동화 검토 중 |
| Media | 뉴스 | 정기 모니터링 운영 중 |

시트 여백의 메모 (필기 성격, 의미 확인 필요):
- "cm쪽 데이터로 할 수 있는 건 없는지"
- 전체 크롤링 데이터는 사내 SharePoint에 있음 (접근 권한 필요, 링크는 여기 옮기지 않음)
- "토스 x 오아시스: 크롤링 데이터 싹 받아서 … 컬리랑 어디가 더 민감하다면 그쪽 방향으로 하도록"

## 5. 기대 효과

1. 고객 관심과 실제 수요의 관계에 대한 이해 확대
2. 시장 변화 조기 Signal 탐지 체계 구축
3. 성장 및 약화 영역에 대한 설명력 강화
4. 고객 수요 이동을 설명하는 MI Framework 기반 마련

## 6. Open Questions

1. Search, Buyer, Search Buyer를 어떤 단위(Category / Theme / Keyword)로 연결하는 것이 가장 적절한가?
2. Search 데이터를 실제 수요 Signal로 활용할 수 있는 범위는 어디까지인가?
3. Category Health를 구축하기 위한 최소 데이터셋은 무엇인가?
4. Trend Radar Signal 간 가중치와 우선순위는 어떻게 정의할 것인가?

## 7. 기존 데이터 정리 (시트 2)

| 항목 | Format | 출처 | 내용 (예시 이미지 기준) |
|---|---|---|---|
| Search report | Excel | Hive query | 주차별(예: 2026 W43) 키워드 순위. Search 수/WoW/YoY, 구매자 수/CVR/WoW/YoY, Fresh vs 3P GMV·판매수량·점유율, Searches per Searcher |
| 경쟁사 상품 정보 | Excel | (비어 있음) | 상품 단위 크롤링 표: 코드, 상품명, 배송유형, 브랜드, 카테고리, 가격, URL, 옵션, 프로모션, 리뷰 수, 매핑 여부 등 |
| 경쟁사 전단 | (비어 있음) | 전단광고 | 전단 이미지 모음 (OCR 추출 대상) |
| 뉴스 모니터링 | URL | (비어 있음) | 뉴스 모니터링 툴(키워드 그룹별) 화면, Fresh 뉴스 브리핑 문서 |
| 쿠팡 내부 데이터 | Power BI | (비어 있음) | 월별(yyyymm) Customer 수, spend, 구매 빈도, AOU, AOV, GMV, unitsold, ASP |

## 8. MarketFlow 구현 현황

MarketFlow 안의 모듈(`src/marketflow/mi/`)로 구현했습니다. 사용법은 README의 "Market intelligence", 요구사항은 SRS F-06 참고.

| MI Framework | 구현 |
|---|---|
| Category Health Dashboard | 대시보드 "Category health" 탭: 수요(GMV/판매수량) 증감, 카테고리 간 수요 점유율 이동, 재고 소진일수(기존 재고 데이터), Search 선행성 검증 |
| Food Trend Radar | "Trend radar" 탭: 키워드별 Seed / Emerging / Growing / Mainstream / Fading, 점수와 기회 점수 |
| Search → Demand Leading Indicator 검증 | 변화율 상관을 lag 0~2에서 검정(p<0.01 기준). 검증되지 않으면 "관심이지 수요가 아님"으로 표기 |
| What Changed / Why / What to do | 모든 행에 3문장 |
| Open Question 4 (신호 가중치) | `[mi.weights]` 설정으로 조정 |
| 데이터 입력 | CSV/XLSX 임포트 (`import-mi`, 관리자용 `POST /mi/import`). Hive/Power BI 직접 연동은 하지 않음 |

미구현: 뉴스 자동 수집, 전단 OCR, 컬리 신규상품 크롤링 연동(이들은 CSV로 PRODUCT/PROMOTION/MEDIA 신호를 넣는 방식으로 대체), Opportunity Radar 전용 화면(기회 점수 정렬로 대체).
