// English / Korean strings for the dashboard. Loaded before app.js (plain scripts share one global scope).
// Static text is marked in index.html with data-i18n="key"; dynamic text goes through t(key, params).
// Server-written text (market-intelligence narratives) is requested in the active language via ?lang=.
const LANGS = ["en", "ko"];
const MSG = {
  en: {
    "summary": "Summary", "login.email": "Email", "login.password": "Password", "login.submit": "Sign in", "signout": "Sign out", "lang.switch": "한국어",
    "tab.matrix": "Comparison matrix", "tab.health": "Category health", "tab.radar": "Trend radar", "tab.recs": "Recommendations",
    "tab.review": "Review queue", "tab.selectors": "Selector candidates",
    "watch_only": "Watchlist only", "matrix.hint": "Charts show the last 14 days. Hover a chart for daily values.",
    "th.item": "Item", "th.channel": "Channel", "th.wholesale": "Wholesale", "th.per_unit": "per standard unit", "th.ours": "Our price",
    "th.margin": "Margin", "th.lowest": "Lowest competitor", "th.pack": "pack price", "th.stock": "Stock", "th.watch": "Watch",
    "th.num": "#", "th.current": "Current", "th.suggested": "Suggested", "th.reason": "Reason", "th.flags": "Flags", "th.status": "Status",
    "th.kind": "Kind", "th.source": "Source", "th.platform": "Platform", "th.title": "Title", "th.price": "Price",
    "th.category": "Category", "th.demand": "Demand", "th.interest": "Search interest", "th.share": "Share of demand", "th.cover": "Stock cover",
    "th.story": "What changed · why · what to do", "th.keyword": "Keyword", "th.stage": "Stage", "th.searches": "Searches",
    "th.other": "Other signals", "th.score": "Score",
    "export": "Export applied prices (CSV)",
    "health.hint": "Demand is real sales (GMV or units sold); search is shown as interest only.",
    "radar.hint": "Keywords gaining interest, ranked by opportunity: rising, still small, and backed by other signals. Interest is not demand.",
    "radar.stable": "Include flat keywords",
    "sum.items": "items tracked", "sum.above": "priced above the lowest competitor", "sum.oos": "competitor listings out of stock",
    "sum.pending": "recommendations awaiting a decision",
    "chart.over": "{arrow} {delta} over {d}d", "chart.one_day": "1 day of data", "chart.none": "No history",
    "chart.aria": "{name}, last {n} days: {points}", "chart.margin": "Margin %",
    "empty.watch": "Nothing on your watchlist yet. Star an item to follow it here.",
    "empty.items": "No items yet. Import inventory with the import-erp command to see them here.",
    "empty.recs": "No recommendations yet. They appear after the next scheduled decision run.",
    "empty.review": "Nothing to review. Items the system cannot match or parse will show up here.",
    "empty.selectors": "No selector proposals. When a retailer changes its page, the system proposes new selectors here.",
    "empty.health": "No market-signal data yet. Import buyer or SKU sales with: python -m marketflow.cli import-mi FILE.csv --type BUYER",
    "empty.radar": "Nothing on the radar yet. Import weekly keyword searches with: python -m marketflow.cli import-mi FILE.csv --type SEARCH",
    "oos": "{n}/{total} out of stock", "watch.add": "Add {name} to watchlist", "watch.remove": "Remove {name} from watchlist",
    "act.approve": "approve", "act.reject": "reject", "act.apply": "apply", "act.revert": "revert", "act.resolve": "resolve",
    "session_expired": "Session expired",
    "stage.SEED": "◔ Seed", "stage.EMERGING": "◑ Emerging", "stage.GROWING": "▲ Growing", "stage.MAINSTREAM": "● Mainstream", "stage.FADING": "▼ Fading", "stage.STABLE": "▬ Flat",
    "demand.GROWING": "▲ Rising", "demand.WEAKENING": "▼ Falling", "demand.STEADY": "▬ Steady", "demand.NO_DATA": "No data",
    "story.changed": "What changed", "story.why": "Why it matters", "story.action": "What to do",
    "metric.gmv": "GMV", "metric.unitsold": "Units sold", "metric.default": "Demand", "searches": "searches",
    "vs_earlier": "{pct} vs earlier", "no_category": "No category", "none_yet": "None yet", "no_sales": "No sales",
    "cover": "{days} days · {status}", "supply.TIGHT": "tight", "supply.BALANCED": "balanced", "supply.SURPLUS": "surplus", "supply.NO_SALES": "no sales",
    "signal.product": "Product", "signal.promotion": "Promotion", "signal.media": "Media",
    "share_shift": " ({pp} pt)",
    "code.PENDING": "Pending", "code.APPROVED": "Approved", "code.REJECTED": "Rejected", "code.APPLIED": "Applied", "code.SUPPRESSED": "Suppressed", "code.REVERTED": "Reverted",
    "code.REVERSE_MARGIN_WARNING": "Reverse margin warning", "code.SPOILAGE_RISK_FLAG": "Spoilage risk", "code.LOW_MARGIN": "Low margin",
    "code.WHOLESALE_GUARD": "Wholesale guard", "code.COOLDOWN_HOLD": "Cool-down hold",
    "code.COMPETITIVE_MATCH": "Competitive match", "code.SPOILAGE_RISK": "Spoilage risk", "code.SUPPLY_VACUUM": "Supply vacuum", "code.REVERSE_MARGIN": "Reverse margin",
    "code.FLASH_COUPON": "Flash coupon", "code.REROUTE_OFFLINE_MARKDOWN": "Reroute / offline markdown", "code.NO_CHANGE": "No change",
    "code.UNMATCHED_TITLE": "Unmatched title", "code.UNPARSEABLE_TITLE": "Unparseable title", "code.QUARANTINED_LISTING": "Quarantined listing",
    "code.UNMAPPED_BENCHMARK": "Unmapped benchmark", "code.UNCONVERTIBLE_BENCHMARK": "Unconvertible benchmark",
  },
  ko: {
    "summary": "요약", "login.email": "이메일", "login.password": "비밀번호", "login.submit": "로그인", "signout": "로그아웃", "lang.switch": "English",
    "tab.matrix": "비교 매트릭스", "tab.health": "카테고리 헬스", "tab.radar": "트렌드 레이더", "tab.recs": "가격 추천",
    "tab.review": "검토 큐", "tab.selectors": "셀렉터 후보",
    "watch_only": "관심 품목만", "matrix.hint": "차트는 최근 14일입니다. 차트에 마우스를 올리면 일별 값이 보입니다.",
    "th.item": "품목", "th.channel": "채널", "th.wholesale": "도매가", "th.per_unit": "표준 단위당", "th.ours": "우리 가격",
    "th.margin": "마진", "th.lowest": "경쟁사 최저가", "th.pack": "팩 가격", "th.stock": "재고", "th.watch": "관심",
    "th.num": "#", "th.current": "현재", "th.suggested": "제안", "th.reason": "사유", "th.flags": "경고", "th.status": "상태",
    "th.kind": "종류", "th.source": "출처", "th.platform": "플랫폼", "th.title": "제목", "th.price": "가격",
    "th.category": "카테고리", "th.demand": "수요", "th.interest": "검색 관심도", "th.share": "수요 비중", "th.cover": "재고 소진일",
    "th.story": "무엇이 바뀌었나 · 왜 중요한가 · 무엇을 할까", "th.keyword": "키워드", "th.stage": "단계", "th.searches": "검색",
    "th.other": "다른 신호", "th.score": "점수",
    "export": "적용된 가격 내보내기 (CSV)",
    "health.hint": "수요는 실제 판매(GMV 또는 판매수량)이며, 검색은 관심으로만 표시합니다.",
    "radar.hint": "관심이 늘고 있는 키워드를 기회 순으로 보여줍니다. 오르는 중이고, 아직 작고, 다른 신호의 뒷받침이 있는 키워드입니다. 관심은 수요가 아닙니다.",
    "radar.stable": "보합 키워드 포함",
    "sum.items": "추적 품목", "sum.above": "경쟁사 최저가보다 높은 품목", "sum.oos": "품절된 경쟁사 상품",
    "sum.pending": "결정 대기 중인 추천",
    "chart.over": "{arrow} {delta} / {d}일", "chart.one_day": "데이터 1일", "chart.none": "이력 없음",
    "chart.aria": "{name}, 최근 {n}일: {points}", "chart.margin": "마진율 %",
    "empty.watch": "아직 관심 품목이 없습니다. 별표를 눌러 이곳에서 따라가세요.",
    "empty.items": "아직 품목이 없습니다. import-erp 명령으로 재고를 가져오면 여기에 표시됩니다.",
    "empty.recs": "아직 추천이 없습니다. 다음 정기 결정 실행 후 나타납니다.",
    "empty.review": "검토할 항목이 없습니다. 시스템이 매칭하거나 해석하지 못한 항목이 여기에 나타납니다.",
    "empty.selectors": "셀렉터 제안이 없습니다. 판매처가 페이지를 바꾸면 시스템이 새 셀렉터를 여기에 제안합니다.",
    "empty.health": "아직 시장 신호 데이터가 없습니다. 구매자 또는 SKU 판매 데이터를 가져오세요: python -m marketflow.cli import-mi FILE.csv --type BUYER",
    "empty.radar": "아직 레이더에 올라온 것이 없습니다. 주간 키워드 검색 데이터를 가져오세요: python -m marketflow.cli import-mi FILE.csv --type SEARCH",
    "oos": "{n}/{total} 품절", "watch.add": "{name}을(를) 관심 품목에 추가", "watch.remove": "{name}을(를) 관심 품목에서 제거",
    "act.approve": "승인", "act.reject": "반려", "act.apply": "적용", "act.revert": "되돌리기", "act.resolve": "해결",
    "session_expired": "세션이 만료되었습니다",
    "stage.SEED": "◔ 씨앗", "stage.EMERGING": "◑ 부상", "stage.GROWING": "▲ 성장", "stage.MAINSTREAM": "● 주류", "stage.FADING": "▼ 쇠퇴", "stage.STABLE": "▬ 보합",
    "demand.GROWING": "▲ 증가", "demand.WEAKENING": "▼ 감소", "demand.STEADY": "▬ 보합", "demand.NO_DATA": "데이터 없음",
    "story.changed": "무엇이 바뀌었나", "story.why": "왜 중요한가", "story.action": "무엇을 할까",
    "metric.gmv": "GMV", "metric.unitsold": "판매수량", "metric.default": "수요", "searches": "검색",
    "vs_earlier": "{pct} (이전 대비)", "no_category": "카테고리 없음", "none_yet": "아직 없음", "no_sales": "판매 없음",
    "cover": "{days}일 · {status}", "supply.TIGHT": "빠듯", "supply.BALANCED": "적정", "supply.SURPLUS": "여유", "supply.NO_SALES": "판매 없음",
    "signal.product": "상품", "signal.promotion": "프로모션", "signal.media": "미디어",
    "share_shift": " ({pp}%p)",
    "code.PENDING": "대기", "code.APPROVED": "승인됨", "code.REJECTED": "반려됨", "code.APPLIED": "적용됨", "code.SUPPRESSED": "보류", "code.REVERTED": "되돌림",
    "code.REVERSE_MARGIN_WARNING": "역마진 경고", "code.SPOILAGE_RISK_FLAG": "폐기 위험", "code.LOW_MARGIN": "낮은 마진",
    "code.WHOLESALE_GUARD": "도매가 가드", "code.COOLDOWN_HOLD": "쿨다운 유지",
    "code.COMPETITIVE_MATCH": "경쟁사 대응", "code.SPOILAGE_RISK": "폐기 위험", "code.SUPPLY_VACUUM": "공급 공백", "code.REVERSE_MARGIN": "역마진",
    "code.FLASH_COUPON": "플래시 쿠폰", "code.REROUTE_OFFLINE_MARKDOWN": "경로 전환 / 오프라인 할인", "code.NO_CHANGE": "변경 없음",
    "code.UNMATCHED_TITLE": "매칭 안 된 상품명", "code.UNPARSEABLE_TITLE": "해석 불가 상품명", "code.QUARANTINED_LISTING": "격리된 상품",
    "code.UNMAPPED_BENCHMARK": "미매핑 시세", "code.UNCONVERTIBLE_BENCHMARK": "변환 불가 시세",
  },
};
let lang = (() => {
  try { const s = localStorage.getItem("mf-lang"); if (LANGS.includes(s)) return s; } catch (e) { /* storage blocked: fall through */ }
  return String(navigator.language || "").toLowerCase().startsWith("ko") ? "ko" : "en";
})();
const t = (key, params) => {
  let s = MSG[lang][key] ?? MSG.en[key] ?? key;
  if (params) for (const [k, v] of Object.entries(params)) s = s.split("{" + k + "}").join(String(v));
  return s;
};
// Known codes (statuses, flags, reasons, kinds) have translations; anything else falls back to "Some code".
const codeText = (c) => MSG[lang]["code." + c] ?? MSG.en["code." + c] ?? (String(c).replace(/_/g, " ").toLowerCase().replace(/^./, (x) => x.toUpperCase()));
const numLocale = () => (lang === "ko" ? "ko-KR" : "en-US");
function applyStatic() {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach((e) => { e.textContent = t(e.dataset.i18n); });
  document.querySelectorAll("[data-i18n-label]").forEach((e) => e.setAttribute("aria-label", t(e.dataset.i18nLabel)));
}
