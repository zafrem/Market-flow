# MarketFlow 운영 플레이북

MarketFlow를 운영하고 문제를 해결하는 절차입니다. 구성 요소가 어떻게 맞물리는지는 [ARCHITECTURE.md](ARCHITECTURE.md)에 있습니다. 명령은 저장소 루트에서 실행하는 것으로 적었습니다. 로컬에서는 `.venv/bin/python`, Docker에서는 `docker compose exec backend-api python`을 씁니다.

## 1. 처음 설치

### 로컬 (DuckDB)
1. `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
2. `cp config/settings.example.toml config/settings.toml && chmod 600 config/settings.toml`
3. 비밀 값을 만듭니다: `python -c "import secrets; print(secrets.token_urlsafe(48))"` → `[auth] jwt_secret`.
4. `python -m marketflow.worker --check`로 설정 상태를 봅니다(토큰은 가려서 표시).
5. 첫 관리자를 만듭니다: `python -m marketflow.cli add-user admin@example.com "Admin" ADMIN` (비밀번호를 묻습니다, 10자 이상).
6. API(`python -m marketflow.web`)와 워커(`python -m marketflow.worker`)를 시작합니다.

### Docker
1. `cp .env.example .env`를 하고 `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`, `JWT_SECRET`을 설정합니다(`JWT_SECRET`이 없으면 compose가 시작하지 않습니다).
2. `docker compose up --build -d`
3. `docker compose exec backend-api python -m marketflow.cli add-user admin@example.com "Admin" ADMIN`
4. http://127.0.0.1:8000 을 엽니다. 원격 접속은 앞에 TLS 리버스 프록시를 두고 `hsts = true`와 `trust_proxy`를 설정합니다.
5. `collector-worker`는 정확히 하나만 실행합니다.

## 2. 사람 등록

| 작업 | 명령 |
|---|---|
| MD나 바이어 추가 | `cli add-user kim@example.com "Kim" MD` (역할: `ADMIN`, `MD`, `BUYER`) |
| 카테고리 범위 부여 | `cli assign kim@example.com Fresh/Fruit` (MD/BUYER는 배정된 카테고리만 봅니다) |
| 사용자 조회/비활성화, 역할 변경 | ADMIN API `GET /admin/users`와 수정 경로(대시보드에는 사용자 페이지가 없습니다). 다음 요청부터 적용됩니다. |

사용자에게 알려 줄 규칙: 바이어는 원가와 마진을 볼 수 없습니다. MD는 가격을 승인하고 적용하며 크롤러를 볼 수 있습니다. 크롤러 변경과 MI 데이터 가져오기는 관리자만 합니다.

## 3. 일상 운영

### 3.1 아침 점검 (약 5분, 관리자)
1. **비교 매트릭스 위의 상태 띠**(와 `cli quality`): 크롤러마다 칩 하나(정상, 수집 지연, 성공률 낮음)와 미확정 매칭, 재확인할 매칭, 읽지 못한 제목의 건수가 표시됩니다. **크롤러 탭**: 켜진 크롤러는 모두 최근 OK 실행과 최신 데이터 날짜를 보여야 합니다. FAILED나 지연 행은 §6을 봅니다.
2. **검토 큐**(`cli review` 또는 대시보드): 매칭되지 않은 제목과 매핑되지 않은 벤치마크 항목을 처리합니다(§4.2).
3. **셀렉터 후보**(`cli selectors`): 복구된 셀렉터를 승인하거나 무시합니다(§4.3).
4. **추천**: MD가 PENDING 항목을 처리합니다(§3.2).
5. 알림 채널: `CRAWLER_FAILED`, `LOW_MARGIN`, 품절, 오류 알림을 확인합니다.

### 3.2 가격 추천 (MD)
1. 오늘 재고가 자동으로 들어오지 않았다면 가져옵니다: `cli import-internal inventory.xlsx`(SharePoint에서 받은 Excel) 또는 `cli import-hive --query inventory` (CSV 또는 XLSX, 잘못된 행은 줄 번호와 함께 나오며, 고쳐서 다시 가져옵니다).
2. 추천은 `decide` 작업(수집 30분 뒤) 뒤에 나타나거나 `cli decide`로 바로 만듭니다. 다시 실행해도 안전하며 중복이 생기지 않습니다.
3. 대시보드에서 PENDING 행을 하나씩 봅니다: 현재 가격 대 제안 가격, 사유 코드, 플래그(역마진, 폐기 위험, 오래된 데이터).
4. `approve` 후 `apply`, 또는 `reject`. CLI: `cli approve 3 --as kim:MD`, `cli apply 3 --as kim:MD`.
5. 적용은 내부 장부만 갱신합니다. 채널 담당자에게 줄 변경 목록은 `cli export-prices --since 2026-11-01 --file changes.csv`로 내보내고, 가격은 판매 채널에 직접 반영합니다.
6. 실수했다면 `cli revert ID --as kim:MD` (감사 기록).

### 3.2a 직접 가격 제안하기, 혼자 쓰기
매트릭스 행에서 **가격 제안**을 누르고 가격과 사유를 입력합니다. 제안에는 근거 스냅샷(원가, 경쟁사 가격, 데이터 시각)이 저장됩니다. 시스템을 혼자 운영하면(기본값 `[approval] solo_mode = true`) 자기 제안을 직접 승인하고 적용할 수 있으며, 마진 방어선 아래 제안도 마찬가지입니다(플래그와 감사 기록은 남습니다). 팀이 쓸 때는 `solo_mode = false`로 바꿉니다. 그러면 작성자는 자기 제안을 승인할 수 없고, 방어선 아래 제안은 ADMIN이 승인해야 합니다. 과거 어느 날에 무엇을 알고 있었는지 보려면 `cli snapshot YYYY-MM-DD`(지금 아는 대로) 또는 `--known-at <UTC 시각>`(그때 시스템이 알던 대로)을 씁니다.

### 3.2b 프로모션
대시보드 > 프로모션: 행사(이름, 기간, 카테고리, 채널, 할인율)를 기록해 검색이나 판매의 급등을 그 일정과 함께 읽습니다. 가격은 바꾸지 않습니다. 모든 카테고리에 대한 프로모션 기록은 관리자만 할 수 있습니다.

### 3.2c 전단과 인사이트
`data/inbox/flyer/`에 넣은 전단 이미지는 전단 크롤러가 실행될 때 읽습니다. 대시보드 > 전단(탭 배지 = 대기 중인 전단 수): 이미지와 항목을 비교하고, 이름과 가격을 고치고, 승인 또는 반려합니다. 승인된 항목만 분석에 반영됩니다. 대시보드 > 인사이트(또는 `cli insights`)에는 효과 없던 프로모션, 장기 할인 중인 경쟁사 상품, 경쟁사 신상품, 카테고리별 판매 반응이 나옵니다. 조치하기 전에 숫자 옆에 적힌 근거를 먼저 읽으세요.

### 3.3 마켓 인텔리전스 (주간 또는 월간)
1. 데이터를 준비합니다: 검색 리포트(Hive 내보내기) → `report` 크롤러, Power BI 월간 내보내기 → `buyer`, 전단 이미지(PNG/JPEG/WebP, 이미지는 OpenAI로 전송되며 키가 필요) 또는 CSV → `flyer`, 신상품 목록 → `newproduct`.
2. CSV/XLSX를 `data/inbox/<크롤러>/`에 넣거나 해당 크롤러 페이지에서 업로드합니다. 기간당 한 행(선택적으로 카테고리와 키워드)이며 나머지 숫자 열은 지표가 됩니다. 허용하는 기간 형식: `2026-10-26`, `20261026`, `202610`, `2026-W43`. 같은 기간을 다시 가져오면 덮어씁니다.
3. 직접 가져오기도 됩니다: `cli import-mi file.csv --type SEARCH|BUYER|SKU|PRODUCT|PROMOTION|MEDIA`.
4. 결과는 대시보드의 *카테고리 헬스*와 *트렌드 레이더* 탭이나 `cli mi-report --lang ko`로 봅니다.
5. 해석: "관심일 뿐 수요 아님"으로 표시된 검색 신호는 그 카테고리에서 선행 검정을 통과하지 *못한* 것입니다. 이 신호로 재고를 계획하지 마세요. 트렌드 단계 SEED/EMERGING은 지켜볼 초기 신호이고, 뒷받침이 있는 GROWING이 실행 가능한 신호입니다.

## 4. 반복 작업

### 4.0 경쟁사 매칭 확정 (MD, SKU를 등록하는 동안은 매일)
수집한 상품은 *검토 필요*로 들어옵니다. 대시보드 > 상품 매칭(탭 배지 = 대기 중인 수)에서 등급(같은 상품 / 유사 / 대체)을 고르고 확정을 누릅니다. "같은 상품"만 추천, 매트릭스, 알림에 쓰입니다. 대기 중인 상품이 있으면 매트릭스의 경쟁사 가격 아래에 "N건 미확정"이 표시됩니다. 단종되었거나 URL이 바뀐 상품은 새 수집에서 사라지므로 그 매칭을 삭제합니다. CLI 대응 명령: `cli matches --unconfirmed`, `cli match`, `cli unmatch`. 모든 변경은 감사 기록이 남습니다(`MATCH_SET`, `MATCH_REMOVED`).

### 4.1 크롤러 일정이나 설정 변경
크롤러 > 크롤러 선택 > 켜짐 / 시각(KST `HH:MM`) / 설정 수정 > 저장. 재시작은 필요 없습니다. 변경은 감사 기록이 남습니다(`CRAWLER_UPDATED`).

### 4.2 검토 큐
`cli review`로 항목을 보고 `cli resolve ID`로 닫습니다. 흔한 원인: 파서가 매칭하지 못하는 새 상품 제목(`master_product_aliases`에 별칭을 추가하거나 `config/extraction.json`의 파서 규칙을 고친 뒤 처리), 매핑되지 않은 KAMIS/가락시장 항목(채널 매핑에 추가).

### 4.3 셀렉터 후보 (자가 복구)
소매 페이지가 바뀌어 LLM이 새 셀렉터를 제안하면 `PENDING`으로 남습니다. `cli selectors`로 확인하고 샘플 추출이 맞아 보이면 `cli approve-selector ID --as kim:MD`를 실행합니다. 그러면 `config/overrides.json`에 기록됩니다(자동으로 다시 읽음).

### 4.4 뉴스 피드 추가
크롤러 > `news` > 설정에서 RSS/Atom URL(http/https, 공개 호스트만)과 조회 기간을 추가합니다. 추적 키워드는 가져온 MI 데이터에서 옵니다. "지금 실행"으로 확인합니다.

### 4.5 가격 책정이나 MI 파라미터 조정
`config/settings.toml`의 `[decision]`(카테고리별은 `[decision.Fresh]`) 또는 `[mi]` / `[mi.weights]`를 고친 뒤 API와 워커를 재시작합니다. `undercut_pct`, `max_step_pct`, `cooldown_hours`부터 시작하고, 한 번에 하나만 바꿔 다음 날 추천과 비교합니다.

### 4.6 알림 설정
알림 본문은 SKU와 상황만 말하고 가격, 마진, 원가는 적지 않습니다(SRS F-08-4.5). `[server] public_url`을 설정하면 알림에 대시보드 링크가 붙습니다. 마진 때문에 채널을 제한할 필요는 없어졌지만 내부 채널로 두세요.
`[alerts] webhook_url`에 Slack 호환 웹훅을 설정합니다. 시험: `cli alert-test`. 같은 알림 키는 24시간에 한 번만 보내고, 백오프로 재시도한 뒤 `alert_log`에 폐기 처리하며, 한 번에 최대 20건입니다. `[alerts] digest = true`이면 RED보다 낮은 알림이 실행당 요약 한 건으로 나가고 RED는 개별로 나갑니다. 데이터 품질 알림(`DATA_QUALITY:*`)도 같은 실행에서 나오며 임계값은 `[quality]`에 있습니다.

## 5. 배포와 업그레이드

1. 먼저 백업합니다(§7).
2. `git pull`을 하고 `.venv/bin/pytest`를 실행합니다(대시보드가 바뀌었다면 `cd tests/js && npm test`도).
3. Docker: `docker compose up --build -d`. `db/init/*.sql`의 스키마 파일은 **빈** 데이터 디렉터리에서만 실행됩니다. 기존 MySQL에는 `db/migrations/`의 파일을 순서대로 적용합니다(`002` 가격·마진 열, `003` 키워드 테이블, `004` 재고 이력, `005` 프로모션, `006` 전단 검토). 새 테이블은 `db/init/001_schema.sql`의 `CREATE TABLE IF NOT EXISTS` 문으로 만들 수도 있습니다. SQLite는 시작할 때 없는 테이블과 열을 추가합니다.
4. `docker compose logs collector-worker backend-api`에서 오류를 확인하고, 크롤러 탭에서 다음 슬롯을 봅니다.
5. 롤백: 이전 이미지/커밋으로 돌리고, 스키마가 바뀌었다면 백업을 복원합니다.

스키마를 바꿀 때는 `scripts/gen_schema.py`를 수정해 `db/init/001_schema.sql`과 `SQLITE_DDL`을 다시 생성합니다. 따로 손으로 고치지 마세요.

## 6. 문제 해결

| 증상 | 가능한 원인 | 조치 |
|---|---|---|
| API가 시작하지 않음 | `jwt_secret`이 없거나 32자 미만 | `[auth] jwt_secret` / `JWT_SECRET`을 설정합니다. |
| 로그인 불가, "too many attempts" | 해당 이메일이 15분에 5번 실패 | 15분 기다리거나 API를 재시작합니다(제한은 메모리에 있음). 관리자와 비밀번호를 확인합니다. |
| 크롤러가 실행되지 않음 | 꺼져 있음, 시각 없음, 워커가 안 돎, `last_slot`이 이미 지남 | 크롤러 페이지에서 켜짐과 시각을 확인하고, `docker compose ps`, `cli crawler-run ID`로 프로세스 안에서 시험합니다. |
| "지금 실행"이 QUEUED에 머묾 | 워커가 꺼져 있거나 같은 DB를 보지 않음 | 워커 로그를 봅니다. 대기 중인 실행은 약 30초 안에 가져갑니다. |
| 실행이 FAILED, 메시지 "stale" | 실행 중에 워커가 재시작됨(RUNNING이 2시간 넘으면 정리됨) | 다시 실행하고 워커 로그에서 충돌을 확인합니다. |
| 실행이 SKIPPED / "not configured" | KAMIS 키/ID, 가락시장 URL, `legal_review_ref`, 뉴스 피드 누락 | 설정을 채웁니다. `worker --check`를 봅니다. |
| 파일 인박스 크롤러가 아무것도 못 읽음 | CSV/XLSX가 아님, 심볼릭 링크, 20MB 초과, 기간 열 없음 | `data/inbox/<id>/failed/`와 실행 메시지를 봅니다(잘못된 셀은 줄 번호와 함께 나옴). Docker에서는 두 서비스 모두 `./inbox`가 마운트됐는지 확인합니다. |
| 대시보드 업로드 거부 | 5MB 초과, 파일 인박스 크롤러가 아님, 관리자가 아님 | 파일을 나누거나 인박스 폴더를 직접 씁니다. |
| 뉴스 크롤러가 언급 0건 | 추적 키워드가 아직 없음, 피드에 접근 불가, 사설 호스트로 거부됨 | SEARCH 데이터를 먼저 가져오고, 워커 호스트에서 URL을 시험하고, 공개 URL을 씁니다. |
| 경쟁사 가격이 없거나 이상함 | 셀렉터가 깨짐, 추출 오류가 확대됨 | 오류 알림과 `cli selectors`를 보고, 복구 후보를 승인하거나 `config/extraction.json`을 고칩니다. |
| 추천이 없음 | 오늘 재고 없음, 상품 미매칭, 전부 보류됨 | `cli import-internal`를 하고 `cli recs --status SUPPRESSED`로 사유 코드를 확인합니다. |
| 전단 이미지가 읽히지 않음 | OpenAI 키 없음, 전단 크롤러 꺼짐, 5MB 초과, PNG/JPEG/WebP가 아님 | `worker --check`의 `vision_enabled`, 실행 메시지, `data/inbox/flyer/failed/`를 봅니다. |
| 카테고리 헬스가 "관심만" 표시 | 선행 검정 미통과 또는 4기간 미만 | 이력을 더 가져옵니다. 의도된 동작이며 버그가 아닙니다. |
| 바이어에게 카테고리가 안 보임 | 배정이 없음 | `cli assign`. |
| 대시보드 글자가 다른 언어로 나옴 | 브라우저에 저장된 선택 | 헤더의 언어 버튼을 씁니다. 크롤러 실행 메시지는 영어로만 나옵니다. |
| 스키마 수정 뒤 MySQL "reserved word" 오류 | 예약어를 열 이름으로 씀 | 열 이름을 바꿉니다(`stored` → `rows_loaded`에서 겪은 일). |

## 7. 백업, 복원, 보존

- **백업 대상**: MySQL 데이터베이스(`mysqldump --single-transaction marketflow`), `config/settings.toml`, `config/overrides.json`, `inbox/`(처리된 파일이 MI 원본 업로드의 유일한 사본입니다).
- **복원**: 워커와 API를 멈추고 덤프를 복원한 뒤 API, 워커 순으로 시작합니다. 크롤러 탭을 확인합니다. 덤프가 오래됐다면 6시간보다 오래 놓친 슬롯은 설계상 건너뛰므로 크롤러를 수동으로 실행합니다.
- 시간 파티션 테이블은 워커가 시작할 때 미래 파티션을 만듭니다(`ensure_partitions`). 오래된 파티션 삭제는 수동 판단입니다.
- `audit_log`는 추가만 가능합니다. 보존 정책을 정하기 전에는 지우지 마세요.

## 8. 보안 점검

- OpenAI 키(`[vision] openai_api_key` / `OPENAI_API_KEY`)는 비용이 드는 자격 증명입니다. `settings.toml`(권한 600)이나 `.env`에만 두고, 유출이 의심되면 OpenAI 콘솔에서 교체하고, 그곳에 사용 한도를 설정합니다.
- 유출이 의심되면 `jwt_secret`을 교체합니다(모든 사용자가 다시 로그인해야 함). KAMIS/가락시장/Gemini 키와 웹훅 URL은 저장소가 아니라 `settings.toml`에서 교체합니다(`config/settings.toml`은 git 제외 파일).
- 분기마다: 활성 사용자와 카테고리 배정을 검토하고, `cli audit`(또는 `GET /admin/audit`)에서 예상 밖의 승인이나 크롤러 변경이 없는지 봅니다.
- 알림 채널은 내부용으로 둡니다. 알림에 수치는 없지만 SKU와 상황은 나옵니다.
- 약관/법무 검토가 `legal_review_ref`에 기록되기 전에는 소매 `SCRAPE` 커넥터를 켜지 마세요.
- 감사 기록에 남기고 싶지 않은 피드나 자격 증명을 크롤러 설정에 넣지 마세요.

## 9. 장애 대응 빠른 참조

| 상황 | 처음 10분 |
|---|---|
| 잘못된 가격이 적용됨 | `cli revert ID --as you:MD`, 바로잡은 변경 목록을 내보내고 채널 담당자에게 알립니다. |
| 잘못된 MI 데이터를 가져옴 | 같은 기간의 고친 파일을 다시 가져옵니다(덮어씀). `signal_type`이 틀렸다면 해당 출처의 `mi_observations` 행을 지우고 다시 가져옵니다. |
| 자격 증명 유출 | 비밀 값을 교체하고(§8), 노출 시점부터의 `audit_log`를 검토합니다. |
| 워커가 계속 죽고 재시작함 | `docker compose logs collector-worker`를 보고, 문제의 크롤러를 페이지에서 끄거나(`UPDATE crawler_settings SET enabled=0`) 반복을 멈춘 뒤 고칩니다. |
| 알림 폭주 | 알림은 실행당 20건으로 제한되고 요약이 나갑니다. 채널을 음소거하지 말고 근본 원인(대개 실패하는 수집기 하나)을 고칩니다. |
| 데이터베이스가 가득 참 / 느림 | 파티션 테이블(`ext_retail_prices`)부터 확인하고, 백업 뒤 오래된 파티션을 보관 처리합니다. |
