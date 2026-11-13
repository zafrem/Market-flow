# MarketFlow 아키텍처

요구사항은 [SRS.md](SRS.md), 일상 운영 절차는 [PLAYBOOK.md](PLAYBOOK.md)에 있습니다. 이 문서는 시스템이 어떻게 구성되어 있고 왜 그렇게 했는지를 설명합니다.

## 1. 시스템 구성

배포 단위 세 개가 데이터베이스 하나를 공유합니다. 메시지 브로커도 캐시도 없습니다. 중복 제거, 작업 상태, 실행 대기열, 알림 이력은 모두 데이터베이스에 있습니다.

```
                 ┌──────────────────────────── collector-worker (1개) ──────────────────────────────────┐
 KAMIS / 가락시장 ▶│ CrawlerService.loop (30초마다) ── 크롤러별 일정과 "지금 실행" 요청을 처리               │
 RSS / Atom   ──▶│ Scheduler ── "decide" 작업 (수집 30분 뒤) ──▶ 의사결정 엔진                            │
 소매 사이트   ──▶│ Notifier ── Slack 호환 웹훅 (중복 제거, 백오프, 폐기)                                  │
 data/inbox/* ──▶└──────────────┬───────────────────────────────────────────────────────────────────────┘
                                │ 기록
                         ┌──────▼───────┐         ┌──────────────────────────────┐
  ERP CSV/XLSX ─ cli ───▶│  MySQL 8 /   │◀────────│ backend-api (FastAPI + 정적   │◀── 브라우저 (MD / BUYER / ADMIN)
  MI CSV/XLSX  ─ cli ───▶│  SQLite(개발)│  읽기   │ 대시보드, 같은 프로세스)       │
                         └──────────────┘         └──────────────┬───────────────┘
                                                                 │ ./inbox 볼륨을 워커와 공유
```

| 구성 요소 | 진입점 | 역할 |
|---|---|---|
| `db` | MySQL 8 (`db/init/001_schema.sql`) | 기록의 원천. SQLite는 로컬 개발 전용입니다(파티션 없음, NUMERIC이 정확한 소수가 아님). |
| `collector-worker` | `python -m marketflow.worker` | 크롤러를 일정대로 실행하고, 의사결정 작업을 돌리고, 알림을 보냅니다. **하나만** 실행합니다. |
| `backend-api` | `python -m marketflow.web` | REST API와 단일 페이지 대시보드. 외부 사이트를 직접 가져오지 않습니다. |
| CLI | `python -m marketflow.cli` | 운영 작업: 가져오기, 결정/승인/적용, 사용자, 감사, 크롤러 단발 실행. |

## 2. 코드 구조 (`src/marketflow/`)

| 패키지 / 모듈 | 역할 |
|---|---|
| `settings.py`, `config/settings.toml` | 계층형 설정: TOML 파일을 환경변수가 덮어씁니다. `--check`에서는 비밀 값을 가립니다. |
| `db.py`, `repository.py`, `ops_repo.py` | SQLAlchemy 2 엔진/DDL(MySQL, SQLite)과 리포지토리. `SQLITE_DDL`은 `scripts/gen_schema.py`가 `db/init/001_schema.sql`과 같은 모델에서 생성합니다. |
| `collectors/` | `KamisCollector`, `GarakCollector`: 공개 도매 벤치마크를 `ext_public_benchmarks`에 넣습니다. |
| `scrape/` | 소매 스크래핑: 전송, 사람처럼 보이는 간격, HTML 정제, `selfheal` 추출기(LLM이 복구한 셀렉터). |
| `title_parser.py`, `units.py`, `curation.py` | 상품 제목 파싱, 단위 가격 정규화, 채널 상품을 `master_products`에 매핑. |
| `erp.py` | 재고/원가 가져오기(CSV/XLSX, 영어/한국어 헤더, 오류는 줄 번호로 보고). |
| `decision/` | `calc.py`(가격 계산), `engine.py`(추천 규칙), `service.py`(실행, 승인, 반려, 적용, 되돌리기, 감사 기록), `config.py`. |
| `mi/` | 마켓 인텔리전스: 가져오기, 저장소, `analysis.py`(카테고리 헬스, 트렌드 레이더), 이중 언어 `messages.py`. |
| `crawlers/` | 데이터 항목마다 크롤러 하나를 두는 프레임워크: `base.py`(규약), `adapters.py`, `store.py`, `service.py`. |
| `scheduler.py`, `schedule.py` | KST 슬롯 계산(`latest_slot`)과 `decide` 작업. |
| `notify.py` | 알림 생성, 중복 제거, 재시도, 폐기. |
| `web/` | `app.py`(라우트), `repo.py`(조회 모델), `security.py`(해시, JWT, 제한), `static/`(대시보드: `index.html`, `app.js`, `i18n.js`, `style.css`). |
| `matching.py`, `promotions.py`, `quality.py`, `snapshot.py`, `vision.py`, `flyers.py`, `insights.py` | 매칭 등급(감사 기록), 프로모션 캘린더, 일일 데이터 품질 보고와 알림, 특정 시점 스냅샷(발생 시각 대 적재 시각), OpenAI API로 전단 읽기, 전단 검토(항목은 사람이 확인하기 전에는 분석에 쓰이지 않음), P2 분석(프로모션 성과, 장기 할인, 신상품, 판매 반응 시나리오). |
| `mi/` (추가) | `privacy.py`(가져오기 때 개인정보 필터), `classify.py`(카테고리 분류기), `gap.py`(검색-구매 간극 진단), `store.py`의 동의어 사전. |
| `time_provider.py` | 주입 가능한 시계. 모든 비즈니스 로직이 `TimeProvider`를 받으므로 테스트는 고정된 시각에서 돌아갑니다. |

## 3. 데이터 모델

`db/init/001_schema.sql`에 정의되어 있습니다.

| 그룹 | 테이블 | 비고 |
|---|---|---|
| 카탈로그 | `master_products`, `master_product_aliases`, `product_channel_map` | `std_item_id`가 모든 출처를 잇는 키입니다. |
| 시장 데이터 | `ext_public_benchmarks`, `ext_retail_prices` | MySQL에서는 시간 기준 파티션입니다(`partitions.py`가 미래 파티션을 계속 만들어 둡니다). |
| 내부 | `int_operations_inventory`, `inventory_revisions` | ERP 가져오기: 재고, 원가, 가격. 적재할 때마다 적재 시각이 찍힌 이력이 추가됩니다. 원가 데이터는 BUYER 응답에 나가지 않습니다. |
| 의사결정 | `price_recommendations` | 상태: PENDING → APPROVED → APPLIED, 또는 SUPPRESSED / REJECTED / REVERTED. 사람이 만든 제안(초안)은 `flags`에 근거 스냅샷을 담습니다. |
| 운영 | `review_queue`, `alert_log`, `selector_candidates`, `audit_log` | 검토 항목과 알림은 중복을 제거합니다. |
| 접근 | `users`, `user_category_assignments`, `user_watchlists`, `user_preferences` | |
| 마켓 인텔리전스 | `mi_observations` | 롱 포맷: `signal_type`, `period_start`, `category_l`, `keyword`, `metric`, `value`, `source`. 이 키에 유일 제약이 있습니다. |
| 키워드와 캘린더 | `mi_synonyms`, `promotions` | 이력이 있는 동의어 사전, 프로모션 캘린더(참고용). |
| 전단 | `flyers`, `flyer_items` | 이미지에서 읽은 항목. APPROVED만 PROMOTION 관측값이 됩니다. |
| 크롤러 | `crawler_settings`, `crawler_runs` | 설정(켜짐, KST 시각, JSON 설정, `last_slot`)과 상태, `rows_loaded`가 있는 실행 이력. |

## 4. 주요 흐름

### 4.1 가격 추천
1. 수집기와 소매 크롤러가 벤치마크와 경쟁사 가격을 기록합니다(제목 파서가 `std_item_id`에 매칭하며, 매칭되지 않은 제목은 `review_queue`로 갑니다).
2. `import-erp`가 재고와 원가를 적재합니다.
3. 사람이 SAME으로 확정한 경쟁사 상품만 반영합니다(`product_channel_map.reviewed_at`). 시뮬레이터는 맞춘 실구매가에서의 예상 마진과 손익분기 수량을 더합니다. `decide` 작업(`cli decide`도 동일)은 재고 행마다 평가합니다: 목표 마진과 VAT로 바닥가를 계산하고, 최저 경쟁사에 맞추거나 더 싸게 하며, 한 번에 움직이는 폭을 제한하고(`max_step_pct`), 쿨다운을 적용하고, 도매가가 오르는 동안은 인하를 보류하고, 폐기 위험이 있으면 인하합니다. 결과는 사유 코드와 플래그가 붙은 `price_recommendations` 행이거나, 사유가 붙은 `SUPPRESSED`입니다.
4. MD/ADMIN이 승인합니다(혼자 쓰기 모드: 제안자가 자기 제안을 승인할 수 있음. 팀 모드: 작성자 ≠ 승인자이며 마진 방어선 아래는 ADMIN). `apply`는 내부 장부만 갱신합니다. **판매 채널에는 아무것도 쓰지 않습니다.** 엔진은 보조 역할이며, `export-prices`가 변경 목록을 만듭니다.
5. 모든 전이는 원자적 상태 갱신(compare-and-set)이며 `audit_log`에 기록됩니다.

### 4.2 크롤러
- `crawlers/base.py`가 `Crawler`(id, 라벨, 설정 `Field` 스키마, `RunOutcome`을 반환하는 `run()`)를 정의합니다. `NotConfigured`는 아직 실행할 수 없는 크롤러를 뜻합니다(실패가 아니라 SKIPPED).
- 데이터 항목마다 하나씩, 크롤러는 여덟 개입니다: `report`, `buyer`, `news`, `competitor`, `flyer`, `newproduct`, `benchmark_kamis`, `benchmark_garak`.
- 어댑터: `InboxCrawler`(`data/inbox/<id>/`의 CSV/XLSX), `FlyerCrawler`(이미지는 OpenAI로 읽어 검토 대기), `NewsCrawler`(RSS/Atom을 주간 키워드 언급 수로), `BenchmarkCrawler`, `CompetitorCrawler`.
- `CrawlerService.dispatch_once`가 워커 안에서 30초마다 돕니다: 멈춘 실행 정리, 대기 중인 수동 실행 시작, 도래한 예약 실행 시작. `latest_slot(now, times)`가 `last_slot`보다 새롭고 6시간 만회 구간 안일 때 슬롯이 도래한 것으로 보며, 더 오래 놓친 슬롯은 건너뜁니다.
- 크롤러당 한 번에 하나만 실행합니다(사전 확인 + compare-and-set `UPDATE`). RUNNING이 2시간을 넘으면 FAILED로 정리합니다. 실패하면 `CRAWLER_FAILED` 알림이 나갑니다.
- API는 "지금 실행"(`POST /crawlers/{id}/run`, 202)에 `QUEUED` 행을 만들고 워커가 가져갑니다. API 안에서 직접 실행하는 것은 파일 인박스 크롤러의 업로드 경로뿐입니다.

### 4.3 마켓 인텔리전스
`import-mi` / `POST /mi/import` / 인박스 크롤러가 `mi_observations`를 채웁니다. `mi_report`는 요청마다, 사용자 범위별로 다음을 계산합니다.
- **카테고리 헬스**: 수요 방향(GMV, 없으면 판매수량), 수요 점유율 이동(퍼센트포인트), 재고에서 얻은 재고 커버(TIGHT < 3일, SURPLUS > 14일), 검색→수요 선행 검정(기간 대비 변화율, 시차 0~2, r이 `lead_min_r`와 단측 p < 0.01 임계값을 모두 넘을 때만 인정).
- **트렌드 레이더**: 증가율, 동료 3개 이상 사이의 검색량 백분위, PRODUCT / PROMOTION / MEDIA 신호의 뒷받침으로 생애주기 단계(SEED / EMERGING / GROWING / MAINSTREAM / FADING / STABLE)를 정하고, 가중 점수와 기회 점수를 계산합니다.
- 서술(무엇이 바뀌었나 / 왜 중요한가 / 무엇을 할까)은 서버에서 `en` 또는 `ko`(`?lang=`)로 만들며, 두 문구 목록의 키와 자리표시자가 같은지 테스트합니다.

## 5. 보안 모델

| 항목 | 방법 |
|---|---|
| 인증 | 비밀번호 해시 + 짧은 수명의 JWT(`token_ttl_minutes`). 32자 이상의 `jwt_secret`이 없으면 API가 시작하지 않습니다. 역할은 요청마다 DB에서 다시 읽습니다. |
| 무차별 대입 | `LoginThrottle`: 이메일당 15분에 5번 실패하면 제한. 프로세스 메모리에 있으므로 프로세스별이며 복제본끼리 공유하지 않습니다. |
| 권한 | 역할 ADMIN / MD / BUYER. MD와 BUYER는 배정된 `category_l`만 봅니다. 범위 밖 리소스는 404를 돌려줍니다. BUYER 응답에서는 서버가 원가·마진 키를 뺍니다. 크롤러는 ADMIN이 수정, MD가 조회, BUYER는 접근 불가입니다. 카테고리가 없는 MI 행은 관리자만 봅니다. |
| 전송 | 직접 TLS(`tls_cert`/`tls_key`) 또는 `hsts = true`인 리버스 프록시. `trust_proxy`를 켜면 제한이 실제 클라이언트 IP를 씁니다. |
| 브라우저 | CSP `default-src 'self'; img-src 'self' blob:`, 인라인 스타일/스크립트 없음, 차트는 인라인 SVG, 토큰은 메모리에만 둡니다. |
| 외부 요청 | 뉴스 피드 URL은 http(s)여야 하며 localhost, `.local`, `.internal`, 사설/루프백/링크로컬 IP는 안 됩니다. XML은 `defusedxml`로 파싱합니다. 소매 스크래핑은 기록된 `legal_review_ref`가 있어야 합니다. |
| 파일 입력 | 인박스는 심볼릭 링크를 무시하고 20MB까지, API 업로드는 5MB(MI 가져오기 5MB)까지 받으며, 클라이언트가 준 파일명은 디스크에 쓰지 않습니다. |
| LLM | 보내기 전에 페이지 텍스트를 정제하고, 돌려받은 셀렉터는 페이지와 대조해 검증한 뒤 사람이 승인할 때까지 `PENDING`으로 둡니다. |
| 알림 | 수집한 텍스트는 이스케이프합니다(`@channel` 불가). 알림 본문에는 가격, 마진, 원가 수치가 없고 SKU, 상황, 대시보드 링크만 있습니다. |
| 비전 (OpenAI) | 이미지는 전단 크롤러가 켜져 있고 키가 있을 때만 기기를 떠납니다. 형식은 파일 내용으로 확인하고, 5MB 상한, 출력 검증, 개인정보 제거, 키와 응답 본문은 로그에 남기지 않습니다. 이미지 제공 API는 인박스 안의 파일만 내보냅니다. |
| 승인 | `[approval] solo_mode`(기본 true)는 혼자 쓰는 운영자가 자기 제안을 승인하게 하고, `false`는 작성자 ≠ 승인자와 방어선 아래 ADMIN 승인을 강제합니다. 둘 다 감사 기록이 남습니다. |
| 감사 | 승인/반려/적용/되돌리기, 사용자와 크롤러 변경, 업로드, 가져오기, 전단 검토, 프로모션 변경이 `audit_log`에 기록됩니다. |

## 6. 시간과 스케줄

- 모든 일정은 KST입니다(컨테이너는 `TZ=Asia/Seoul`). 데이터베이스는 UTC로 저장합니다(`--default-time-zone=+00:00`).
- 기본값: 벤치마크 06:00 / 09:00, 소매 08:00 / 13:00 / 19:00, 의사결정은 수집 30분 뒤. 크롤러 시각은 각 크롤러 페이지에서 수정하며 워커 재시작 없이 적용됩니다.
- 비즈니스 코드는 `datetime.now()`를 직접 부르지 않고 `TimeProvider`를 씁니다(테스트에서는 `FixedTimeProvider`).

## 7. 다국어

대시보드는 영어/한국어 이중 언어입니다. 정적 `data-i18n` 속성과 `t(key, params)`를 쓰고, 상태, 플래그, 사유 코드는 `codeText()`를 거치며 모르는 코드는 읽을 수 있는 대체 문구로 표시합니다. 선택은 `localStorage`(`mf-lang`)에 저장하고 기본값은 브라우저 언어입니다. 서버가 만드는 서술은 `?lang=`을 따릅니다. 알려진 한계: 크롤러 실행 메시지는 영어로 저장됩니다.

## 8. 테스트

| 계층 | 명령 | 비고 |
|---|---|---|
| 오프라인 전체 | `.venv/bin/pytest` | SQLite와 픽스처, 네트워크와 토큰 불필요. 가짜 시계로 며칠간의 무인 운영을 시뮬레이션하는 종단 간 자율 운영 테스트(`tests/test_autonomy.py`)를 포함합니다. |
| 대시보드 | `cd tests/js && npm test` (pytest에서도 실행) | 실제 `app.js` + `index.html`을 jsdom에서 가짜 API로 실행합니다. |
| MySQL | `MARKETFLOW_TEST_DB_URL=... pytest -m mysql` | DDL, 파티션, upsert, JSON/ENUM 열, 이력 조회. |
| 실서비스 | `MARKETFLOW_LIVE=1 pytest -m live` | 설정한 토큰으로 실제 KAMIS/가락시장을 호출합니다. |

## 9. 알려진 한계와 확장 지점

- 개정된 가격·마진 기능 명세(SRS F-08): P0, P1, P2를 구현했습니다. 일부만 되었거나 없는 것은 [SRS.md](SRS.md) §4.1에 있습니다.
- 워커는 설계상 하나만 실행합니다. 로그인 제한은 프로세스별입니다.
- Hive와 Power BI에는 직접 커넥터가 없습니다(파일 인박스). 컬리 신상품 수집은 MarketFlow 밖에서 이뤄지며 결과를 인박스에 넣습니다. 전단 이미지는 OpenAI API로 읽습니다(README의 비전 참고).
- CI에서 검증하지 못한 것: Docker 이미지, 새 테이블의 MySQL SQL, 실제 KAMIS/가락시장/Gemini/OpenAI/Slack, 실제 RSS 피드, 실제 브라우저의 대시보드.
- 데이터 출처를 추가하려면: `crawlers/adapters.py`에 `Crawler`를 구현하고, `CrawlerService.registry`에 (`ORDER` 위치와 함께) 등록하고, `web/static/i18n.js`에 라벨 번역을 추가하고, `tests/test_crawlers.py`에 테스트를 추가합니다.
- 가격 규칙을 추가하려면: `decision/engine.py`를 확장하고, 사유 코드를 주고, 그 코드를 `i18n.js`에 추가합니다.
- 채널에 쓰기(write-back)를 추가하려면: `decision/service.py`의 `apply` 뒤에 고유한 감사 동작과 함께 구현합니다. 지금은 없습니다.
