# MarketFlow

**목적:** 도매 시세, 경쟁사 판매가, 내부 원가·재고, 검색·수요 신호를 한곳에 모아 경쟁사와 비교한 가격·마진을 보여 주고, 사람이 승인할 가격 변경안을 제안합니다. 판매 채널에 가격을 직접 쓰지 않는 보조 도구입니다.

| 문서 | 용도 |
|---|---|
| [docs/SRS.md](docs/SRS.md) | 요구사항과 가격·마진 기능의 구현 현황(§4.1) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 구성 요소, 코드 구조, 데이터 모델, 흐름, 보안 |
| [docs/PLAYBOOK.md](docs/PLAYBOOK.md) | 설치, 일상 운영, 문제 해결, 백업, 장애 대응 |
| [docs/MI_FRAMEWORK.md](docs/MI_FRAMEWORK.md) | 마켓 인텔리전스 요구사항 원문 정리 |

## 로컬 실행 (Docker 없이, DuckDB)

**빠른 시작:** `scripts/start.sh` (워커도 함께: `scripts/start.sh --worker`, 다른 포트: `PORT=8765 scripts/start.sh`). 처음 실행하면 가상환경과 의존성, `config/settings.toml`, 로그인 비밀키(`data/.jwt_secret`)를 만들고 첫 ADMIN 계정을 물어본 뒤 http://127.0.0.1:8000 에서 대시보드를 시작합니다. 아래는 같은 일을 손으로 하는 방법입니다.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp config/settings.example.toml config/settings.toml && chmod 600 config/settings.toml
$EDITOR config/settings.toml                # KAMIS 키/ID, 가락시장 URL/키를 넣습니다 (git 제외 파일)

.venv/bin/python -m marketflow.worker --check   # 설정 상태 확인 (토큰은 가려서 표시)
.venv/bin/python -m marketflow.worker --once    # 지금 한 번 수집 -> data/marketflow.duckdb (처음 실행 때 생성·시드)
.venv/bin/python -m marketflow.worker           # 또는 계속 실행하며 KST 일정(06:00, 09:00)에 맞춰 수집
```

모든 설정은 환경변수로 덮어쓸 수 있습니다(`KAMIS_API_KEY`, `KAMIS_API_ID`, `GARAK_API_URL`, `GARAK_API_KEY`, `DATABASE_URL`, `MOCK_LLM`). MySQL을 쓰려면 `database.url`에 `mysql+pymysql://...` 주소를 넣습니다. 로컬 파일 DB(DuckDB 기본, SQLite도 가능)에는 파티션이 없고 날짜와 금액을 텍스트로 저장합니다(아래 "로컬 저장소와 사내 데이터 유입" 참고).

수집 규칙(필드명, 정규식, 파서 진입점)은 `config/extraction.json`에 있고 바뀌면 자동으로 다시 읽습니다. 승인된 수정은 `config/overrides.json`에 저장됩니다.

## 테스트

```bash
.venv/bin/pytest                                   # 오프라인: 픽스처 + 로컬 파일 DB, 토큰 불필요
MARKETFLOW_TEST_BACKEND=duckdb .venv/bin/pytest    # 같은 테스트를 DuckDB 파일 DB로 실행(기본은 SQLite)
MARKETFLOW_LIVE=1 .venv/bin/pytest -m live -s      # 설정한 토큰으로 실제 KAMIS/가락시장 호출
MARKETFLOW_TEST_DB_URL=mysql+pymysql://... .venv/bin/pytest -m mysql   # MySQL DDL/파티션 테스트
```

## 소매 스크래핑과 자가 복구 (2단계)

플랫폼 셀렉터는 `config/extraction.json`의 `retail.<PLATFORM>`에 있습니다. `SCRAPE` 커넥터는 `legal_review_ref`(약관/법무 검토 기록)와 `urls`가 채워져야 실행됩니다.

```bash
.venv/bin/python -m marketflow.worker --retail     # 설정된 플랫폼을 한 번 스크래핑
```

셀렉터가 더 이상 맞지 않으면 페이지를 정제해 LLM에 보냅니다(`settings.toml`에서 `llm.mock=false`와 `gemini_api_key` 설정, 기본값은 오프라인 목). LLM이 돌려준 레코드는 검증하고 페이지 본문과 대조합니다. 검증된 대체 셀렉터는 사람이 `SelfHealingExtractor.approve(platform, actor)`를 호출할 때까지 *대기 후보*로 보관되며, 승인하면 `config/overrides.json`에 기록됩니다. 실제 브라우저 전송은 선택 사항입니다: `pip install -e '.[browser]' && playwright install chromium`.

## 설정 화면: 사용자와 데이터 (ADMIN)

대시보드 왼쪽 메뉴의 **설정**(ADMIN만 보임)에서 두 가지를 합니다.
- **사용자**: 사용자 추가(이메일, 이름, 역할, 담당 카테고리, 비밀번호 10자 이상), 역할 변경, 사용 중지, 카테고리 수정. 자기 자신은 강등하거나 중지할 수 없고, 마지막 활성 ADMIN을 없애는 변경은 서버가 거부합니다. 모든 변경은 감사 기록에 남습니다.
- **데이터**: 저장소 상태(테이블별 행 수)와 두 버튼.
  - **데모 데이터 넣기**: 빈 데이터베이스에만 넣습니다(데이터가 있으면 거부). 도매 시세 60일, 경쟁사 가격 30일(3개 플랫폼), 재고·원가·판매 60일, 프로모션 5건, 검색·구매·프로모션 신호, 매칭(미확정 2건 포함), 오늘 기준 추천 22건을 만듭니다. 가상이지만 그럴듯한 수치이고 항상 같은 값이 나옵니다(고정 시드). 약 1분 걸리며 백그라운드로 돌고 화면이 진행을 보여 줍니다. 카탈로그에 우유·요거트·계란이 추가됩니다.
  - **데이터 비우기…**: `DELETE`를 입력해야 실행됩니다. 가격, 재고·원가 이력, 경쟁사 가격, 매칭, 추천, 프로모션, 수요 신호, 검토 큐, 알림·실행 이력을 지우고, 사용자, 카탈로그, 크롤러 설정, 동의어, 감사 기록은 남깁니다(데모가 추가한 카탈로그 품목은 함께 지움). 되돌릴 수 없고 감사 기록에 `DATA_CLEARED`가 남습니다. 인박스 파일은 건드리지 않습니다.
  - 두 기능은 로컬 파일 DB(DuckDB, SQLite)에서만 동작하며 MySQL에서는 거부합니다.
- API: `GET /admin/system`, `POST /admin/demo`, `POST /admin/clear`(모두 ADMIN만).

## 로컬 저장소와 사내 데이터 유입

- **로컬 저장소는 DuckDB 파일 하나**(`data/marketflow.duckdb`, `[database] url`)입니다. DuckDB 파일은 한 번에 한 프로세스만 열 수 있어서, 워커·웹·CLI는 트랜잭션이 도는 동안만 파일을 열고 다른 프로세스가 놓을 때까지 최대 15초 기다립니다(동시에 써도 유실되지 않음을 4개 프로세스로 확인). 운영(Docker)은 계속 MySQL 8이고, `sqlite:///...`도 그대로 동작합니다.
- **기존 SQLite 데이터 옮기기**: `python -m marketflow.cli migrate-sqlite data/marketflow.db data/marketflow.duckdb`. 새 DuckDB 파일에 스키마를 만들고 모든 테이블을 id 그대로 복사합니다(감사 기록, 이력 포함). 대상 파일이 이미 있으면 덮어쓰지 않고 거부하며, 원본은 건드리지 않습니다.
- **Excel(SharePoint)**: SharePoint에서 `.xlsx`/`.csv`를 내려받아 `import-internal 파일`로 넣습니다(자동 다운로드는 하지 않습니다). 열 이름은 `config/extraction.json`의 `internal.columns` 별칭(한글/영문)으로 맞춥니다.
- **Hive**: `config/settings.toml`의 `[hive]`에 접속 정보와 이름 붙인 SELECT 쿼리를 적고 `pip install -e '.[hive]'` 후 `import-hive --query 이름`을 실행합니다. 비밀번호는 `HIVE_PASSWORD` 환경변수에 두며 로그·오류·`repr`에 나오지 않습니다. 단일 읽기 전용 SELECT만 허용하고(INSERT/DROP 등과 `;` 거부), 한 번에 최대 `max_rows`(기본 20만)행입니다. 쿼리 결과의 열 이름은 파일과 같은 별칭 규칙을 씁니다. 실제 Hive 서버에 대한 연결은 아직 검증하지 못했고, 가짜 연결로 테스트했습니다.

## 사내 데이터 가져오기, 의사결정, 승인 (3단계)

```bash
.venv/bin/python -m marketflow.cli import-internal inventory.xlsx      # .csv 또는 .xlsx(SharePoint에서 받은 Excel), 잘못된 행은 줄 번호로 보고
.venv/bin/python -m marketflow.cli import-hive --query inventory        # Hive를 직접 조회(읽기 전용 SELECT), 같은 검증을 거침
.venv/bin/python -m marketflow.cli decide                         # 오늘 재고 평가 -> PENDING / SUPPRESSED 추천
.venv/bin/python -m marketflow.cli recs --status PENDING
.venv/bin/python -m marketflow.cli approve 3 --as kim:MD          # MD/ADMIN만 가능, 이어서 apply / revert (모두 감사 기록)
.venv/bin/python -m marketflow.cli apply 3 --as kim:MD
.venv/bin/python -m marketflow.cli audit
```

헤더 별칭(영어/한국어)은 `config/extraction.json`의 `internal.columns`에, 가격 책정 파라미터는 `config/settings.toml`의 `[decision]` 표에 있습니다(`settings.example.toml` 참고). 엔진은 보조 역할입니다. `apply`는 내부 장부만 갱신하며 판매 채널에는 아무것도 쓰지 않습니다.

## API와 대시보드 (4단계)

`scripts/start.sh`가 아래 과정(비밀키, 첫 계정, 서버 실행)을 한 번에 처리합니다. 손으로 하려면:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"      # 출력값을 settings.toml -> [auth] jwt_secret에 넣습니다
.venv/bin/python -m marketflow.cli add-user kim@example.com "Kim" MD       # 비밀번호를 묻습니다 (10자 이상)
.venv/bin/python -m marketflow.cli assign kim@example.com Fresh/Fruit      # MD/BUYER는 배정된 카테고리만 봅니다
.venv/bin/python -m marketflow.web                                          # http://127.0.0.1:8000  (강한 jwt_secret이 없으면 시작하지 않음)
```

- `POST /auth/login` -> 베어러 토큰(이메일당 15분에 5번 실패하면 제한). 역할은 요청마다 다시 읽으므로 사용자 비활성화나 역할 변경이 즉시 적용됩니다.
- `GET /matrix[?watchlist=true]` 3자 비교, `GET/PUT /watchlist`, `/preferences`, `GET /recommendations`, `POST /recommendations/{id}/approve|reject|apply|revert`, `/admin/users`, `/admin/audit`(ADMIN만).
- **BUYER 응답에는 원가·마진 키가 아예 없습니다**(서버에서 강제, 테스트로 검증). 범위 밖 리소스는 404를 돌려줍니다.
- 대시보드는 토큰을 메모리에만 둡니다(새로고침하면 로그아웃). 로컬호스트 밖으로 공개할 때는 HTTPS 뒤에서 서비스하세요.

## 가격·마진 (개정 명세, P0)
- **확정된 매칭만 반영합니다.** 수집한 경쟁사 상품은 *미확정* 후보로 저장되고, MD/ADMIN이 **SAME**으로 등급을 매긴 뒤에야 추천, 매트릭스, 알림에 쓰입니다(대시보드 > 상품 매칭, `cli matches --unconfirmed`, `cli match PLATFORM CODE ITEM_ID --as kim:MD [--grade SAME|SIMILAR|SUBSTITUTE]`, `PUT /matches/{platform}/{code}`). SIMILAR/SUBSTITUTE는 기록만 하고 사용하지 않습니다. `[decision]`의 `require_confirmed_match = false`로 이 규칙을 끌 수 있습니다.
- **실구매가.** 경쟁사 비교는 공개 쿠폰, 배송비, 증정 수량(1+1 = 개당 50%)을 반영한 가격으로 합니다. `config/extraction.json`의 선택 셀렉터 필드 `coupon`, `shipping_fee`, `bonus`가 이를 채웁니다. 없으면 표시 가격이 곧 구매가입니다.
- **순마진에 폐기비 포함**: 사내 데이터 열 `disposal_cost`(폐기비, 단위당), 없으면 `[decision] disposal_rate_pct`를 매입가에 적용합니다.
- **맞추면(If we match)**: 매트릭스와 모든 추천에 경쟁사 실구매가로 맞췄을 때의 예상 마진과 주간 손익분기 수량(`주간 수량 x 현재 마진 / 변경 후 마진`)이 표시됩니다. 인하 후 남는 마진이 없으면 "해당 가격에서 마진 없음"으로 표시합니다.
- **내보내기**: 대시보드 > 비교 매트릭스 > 비교표 내보내기(CSV) 또는 `GET /matrix/export`(MD/ADMIN, `MATRIX_EXPORTED`로 감사 기록).
- **알림에는 원가, 마진, 가격 수치가 없습니다.** SKU, 상황, 링크(`[server] public_url`)만 담깁니다. SKU와 유형당 24시간에 한 번만 보냅니다.
- 기존 MySQL 업그레이드: `db/migrations/002`부터 `006`까지 순서대로 적용합니다(가격·마진 열, 키워드 테이블, 재고 이력, 프로모션, 전단 검토). 로컬 파일 DB는 시작할 때 없는 테이블을 스스로 만듭니다(SQLite는 없는 열도 추가, DuckDB는 새 파일 기준).

## 비전 (ChatGPT / OpenAI API)
경쟁사 전단과 배너는 OpenAI API로 읽습니다. 키는 `config/settings.toml`의 `[vision] openai_api_key`(파일 권한 600, git 제외)나 환경변수 `OPENAI_API_KEY`에 넣습니다(`OPENAI_MODEL`은 선택, 기본값 `gpt-4o-mini`, 계정에서 쓸 수 있는 비전 모델이면 됩니다). Docker에서는 `.env`에 `OPENAI_API_KEY`를 설정합니다. `python -m marketflow.worker --check`는 `vision_enabled=True/False`만 출력하고 키는 출력하지 않습니다.

동작 방식: PNG/JPEG/WebP 파일(각 5MB 이하)을 `data/inbox/flyer/`에 넣습니다(배너와 기획전 화면도 이미지이므로 같습니다). 전단 크롤러 일정이나 **지금 실행** 때 각 이미지를 OpenAI에 보내 광고 상품명, 가격, 읽은 쪽의 신뢰도를 받습니다. **아직 분석에는 반영되지 않습니다.** 항목은 **전단** 탭에서 대기하며, 이미지와 항목이 나란히 보이고 확신이 낮은 항목은 표시됩니다. 검토자가 이름이나 가격을 고치고 항목별로 승인 또는 반려하면, 승인된 항목이 그 주 상품당 프로모션 언급 1건이 됩니다(카테고리는 키워드 분류기가 알면 붙음). 나중에 결정을 바꾸면 다시 계산됩니다. 이미지는 `inbox/flyer/review/`에 보관하고 읽지 못한 것은 `failed/`로 옮깁니다. 한 번에 최대 `max_images`(기본 10)장을 처리합니다. CSV/XLSX 전단 파일은 예전처럼 바로 가져옵니다.

외부로 나가는 것: 이미지이며, 전단 크롤러가 켜져 있고 키가 있을 때만 나갑니다. 키가 없으면 이미지는 인박스에 그대로 남습니다. 파일 형식은 확장자가 아니라 파일 내용으로 확인하고, 개인정보(전화번호, 이메일, 주소)가 든 행은 버리며, 키와 API 응답 본문은 로그에 남기지 않습니다. 이 경로는 가짜 API를 쓴 오프라인 테스트로 검증했고, 실제 OpenAI API로는 실행해 보지 않았습니다.

## 마켓 인텔리전스 (카테고리 헬스 + 트렌드 레이더)
CSV/XLSX로 가져온 수요·관심 신호로 만든 대시보드 탭 두 개입니다(`docs/MI_FRAMEWORK.md` 참고).
```bash
python -m marketflow.cli import-mi search_report.csv --type SEARCH     # week,keyword,category,searches,buyers,gmv,unitsold
python -m marketflow.cli import-mi power_bi.csv     --type BUYER      # month,category,gmv,unitsold  (실제 수요)
python -m marketflow.cli import-mi flyers.csv       --type PROMOTION  # week,keyword,category,count   (PRODUCT, MEDIA, SKU도 가능)
python -m marketflow.cli mi-report                                    # 같은 분석을 텍스트로
```
기간당 한 행(선택적으로 카테고리와 키워드)이며, 나머지 숫자 열은 모두 지표가 됩니다. 기간은 `2026-10-26`, `20261026`, `202610`, `2026-W43` 형식을 받습니다. 같은 기간을 다시 가져오면 덮어씁니다. 관리자는 CSV를 본문으로 `POST /mi/import?signal_type=SEARCH`도 쓸 수 있습니다.
- **카테고리 헬스**는 실제 수요(GMV, 없으면 판매수량)가 늘고 있는지, 카테고리 간 수요 점유율이 어떻게 이동했는지, 재고가 따라가는지, 검색이 그 카테고리에서 실제로 수요를 앞섰는지(시차 상관 검정, 아니면 검색은 관심일 뿐이라고 표시)를 알려 줍니다.
- **트렌드 레이더**는 검색 증가율, 카테고리 내 검색량 순위, 상품·프로모션·미디어 신호의 뒷받침 여부로 키워드마다 생애주기 단계(Seed / Emerging / Growing / Mainstream / Fading)를 매깁니다. 가중치와 임계값은 `settings.toml`의 선택 항목 `[mi]`에 있습니다.
- 사용자는 배정된 카테고리만 봅니다. 카테고리가 없는 행은 관리자만 봅니다. 원가 데이터는 쓰지 않습니다.

## 크롤러 (데이터 항목마다 하나, 각자의 페이지)
대시보드 > **크롤러**(관리자는 변경, MD는 조회). 데이터 항목마다 자기 크롤러와 페이지(`#/crawlers/<id>`)가 있고, 켜기/끄기, 일정(KST 시각, 재시작 없이 수정), 설정, 상태, 데이터 신선도, 실행 이력을 볼 수 있습니다. 실패하면 `CRAWLER_FAILED` 알림이 나갑니다.

| 크롤러 | 채우는 것 | 데이터를 얻는 방법 |
|---|---|---|
| `report` | 검색 리포트(SEARCH) | 파일 인박스: Hive 내보내기나 Excel을 CSV/XLSX로 넣거나 페이지에서 업로드(재고·원가는 `import-internal`/`import-hive`) |
| `buyer` | 내부 판매(BUYER) | 파일 인박스(Power BI 내보내기) |
| `news` | 뉴스 언급(MEDIA) | 지정한 RSS/Atom 피드를 읽어 추적 키워드별 주간 기사 수를 셈 |
| `competitor` | 경쟁사 가격 | 자가 복구 셀렉터를 쓰는 소매 스크래퍼(`config/extraction.json`) |
| `flyer`, `newproduct` | 프로모션 / 신상품 신호 | 파일 인박스(전단 이미지는 OpenAI API로 읽음, 컬리 수집은 외부에서 하고 결과를 인박스에 넣음) |
| `benchmark_kamis`, `benchmark_garak` | 공개 도매 가격 | KAMIS / 가락시장 API |

파일 크롤러는 `data/inbox/<id>/*.csv|xlsx`(`[crawlers] inbox_dir`)를 읽고, 파일을 `processed/` 또는 `failed/`로 옮기며, 심볼릭 링크는 무시합니다. 워커(`python -m marketflow.worker`)가 켜진 크롤러를 각자의 시각에 실행하고 "지금 실행" 요청을 약 30초 안에 처리합니다. `python -m marketflow.cli crawlers`로 목록을 보고, `crawler-run ID`로 현재 프로세스에서 하나를 실행합니다. Docker에서는 워커와 API가 `./inbox`를 공유합니다.

## 무인 운영 (스케줄러, 알림, 검토 큐)

```bash
.venv/bin/python -m marketflow.worker      # 벤치마크 06:00/09:00 · 소매 08:00/13:00/19:00 · 의사결정 +30분 (KST)
```

- **알림**은 `[alerts] webhook_url`(Slack 호환)로 갑니다: 역마진, 폐기 위험, 낮은 마진, 경쟁사 품절, 실패한 작업/수집기, 확대된 셀렉터 오류, 새 셀렉터 후보. 같은 키는 24시간에 한 번만 보내고, 백오프(1, 2, 4, 8초)로 재시도한 뒤 `alert_log`에 폐기 처리합니다. 한 번에 최대 20건이며 나머지는 요약 한 건이 됩니다. 수집한 텍스트는 이스케이프해 `@channel` 같은 멘션이 작동하지 않게 합니다. 알림 본문에는 가격, 마진, 원가 수치가 없고 SKU, 상황, 링크만 있습니다. `python -m marketflow.cli alert-test`로 시험합니다.
- **검토 큐**: 매칭하거나 파싱하지 못한 제목, 격리된 LLM 레코드, 매핑되지 않은 벤치마크 항목을 로그에만 남기지 않고 (중복 제거해) 저장합니다. `cli review` / `resolve ID`, 또는 `GET /review`, `POST /review/{id}/resolve`.
- **셀렉터 후보**: LLM이 페이지를 복구하면 검증된 대체 셀렉터가 `PENDING`으로 저장됩니다. `cli selectors`, `approve-selector ID --as kim:MD`(또는 `POST /selectors/{id}/approve`)로 `config/overrides.json`에 기록합니다.

## Docker (전체 스택)

```bash
cp .env.example .env     # MYSQL_ROOT_PASSWORD, MYSQL_PASSWORD, JWT_SECRET 설정 (JWT_SECRET이 없으면 compose가 시작하지 않음)
docker compose up --build -d
docker compose exec backend-api python -m marketflow.cli add-user admin@example.com "Admin" ADMIN
# 대시보드 + API: http://127.0.0.1:8000   (로컬호스트 전용, 원격 접속은 앞에 TLS 리버스 프록시를 둡니다)
```

서비스: `db`(MySQL 8, 스키마는 `db/init`), `collector-worker`(스케줄러), `backend-api`. 상태: 스키마, upsert, 파티션 관리는 실제 MySQL 8.0.46에서 검증했습니다(`MARKETFLOW_TEST_DB_URL`을 쓰는 `-m mysql` 테스트). Docker 이미지 자체는 CI에서 아직 빌드하지 않았습니다. `collector-worker`는 하나만 실행하세요. 중복 제거와 작업 상태는 Redis가 아니라 데이터베이스에 있습니다.

## 대시보드 테스트

```bash
cd tests/js && npm install && npm test      # 실제 app.js + index.html을 jsdom에서 가짜 API로 실행, pytest에서도 실행됨
```

## 혼자 쓰기, 프로모션, 새 화면

- **혼자 쓰기 모드**(`[approval] solo_mode`, 기본 켜짐): 한 사람이 자기 제안을 승인할 수 있고 마진 방어선 아래 제안도 가능합니다(표시와 감사 기록은 그대로). 팀이 쓸 때는 `false`로 바꾸면 작성자는 자기 제안을 승인할 수 없고, 방어선 아래 변경은 ADMIN이 승인해야 합니다.
- **프로모션 캘린더**: 프로모션 탭에서 행사(이름, 기간, 카테고리, 채널, 할인율)를 기록해 검색이나 판매의 급등을 그 일정과 함께 읽습니다. 참고용입니다. 기존 MySQL에는 `db/migrations/005_p1_promotions.sql`을 적용합니다.
- **일일 요약**(`[alerts] digest = true`, 선택): RED보다 낮은 알림을 실행당 한 건의 요약으로 보냅니다. RED는 개별로 나갑니다.
- **데이터 품질 띠**: 비교 매트릭스 위에 크롤러별 칩(정상, 수집 지연, 성공률 낮음)과 사람이 처리해야 할 건수가 표시됩니다.
- 대시보드는 어두운 내비게이션 레일과 요약 띠를 갖고, 숫자는 고정폭 글꼴이라 열이 맞습니다.

## P2 분석 (인사이트 탭)
시스템이 이미 가진 데이터로 계산하며, 모든 결과에 근거 수치가 붙고 데이터가 부족하면 숫자 대신 "데이터 부족"으로 표시합니다. 임계값은 `[insights]`에 있습니다.
- **프로모션 성과**: 프로모션 기간 판매량을 직전 4주 같은 요일 평균과 비교합니다(모든 프로모션 기간은 제외, 계절 보정 없음이며 화면에도 그렇게 적습니다). `min_uplift_pct` 미만이면 효과 없음으로 봅니다.
- **장기 할인 중인 경쟁사**: 90일 중 관측일의 60% 이상 할인한 상품(최소 14일 관측).
- **경쟁사 신상품**: 더 오래 지켜본 플랫폼에서 14일 안에 처음 보인 상품. 이름만 바뀐 상품(제목 글자가 같음)과 같은 카탈로그 품목의 사라진 코드를 대체한 상품은 신상품으로 세지 않습니다.
- **판매 반응**: 해당 카테고리에서 측정된 할인 프로모션으로 탄력성을 구합니다(하위 사분위, 중앙값, 상위 사분위 = 보수, 기본, 낙관). 프로모션이 3건 이상일 때만 보여 주며 건수와 기간을 함께 표시합니다. `GET /scenarios?internal_sku=&channel_name=&price=`는 새 가격에서의 주간 수량과 마진을 예측합니다(MD/ADMIN). CLI: `cli insights`.
- 기존 MySQL 업그레이드: `db/migrations/006_p2_flyers.sql`도 적용합니다.
