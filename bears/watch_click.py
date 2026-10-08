"""NOL 좌석 감시: 잔여수가 SEAT_COUNT 이상인 등급이 생기면 좌석 등급 -> 자동 배정받기 -> 수량 +(SEAT_COUNT회) -> 선택 완료 순서로 클릭.

run.ps1이 디버그 포트(9222)로 띄운 Chrome에 붙어서 동작한다.
이동/로그인/캡차는 직접 하고, 이 스크립트는 열려 있는 모든 탭의 좌석 목록만 지켜본다.
설정은 아래 '설정' 블록에서만 바꾼다. 로그는 콘솔과 watch.log.
"""
# pip install playwright
import re
import time
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error, Page, sync_playwright

# ============================================================================
# 설정 (바꿀 값은 전부 여기)
# ============================================================================
# ---- 경로 ----
BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "watch.log"        # 실행할 때마다 이어서 쌓임
SCREENSHOT_FILE = BASE_DIR / "diag.png"  # 단계 실패 시 화면 저장

# ---- 브라우저 / 타이밍 ----
CDP_URL = "http://localhost:9222"  # run.ps1이 디버그 포트로 띄운 Chrome
POLL_SEC = 0.3                     # 좌석 목록 확인 간격
SEAT_CLICK_TIMEOUT_MS = 3000       # 좌석 등급 클릭 한도
STEP_TIMEOUT_MS = 10_000           # 클릭 후 단계별 버튼 대기 한도

# ---- 좌석 목록 선택자 ----
SEAT_ITEM = 'button[class*="SportsSeatGradeList_contentItem__"]'
# 포함: 등급 이름에 이 글자 중 하나라도 들어간 것만 누름 (비우면 모든 등급이 대상). 예: ["외야", "네이비"]
# 제외: 등급 이름에 이 글자 중 하나라도 들어가면 누르지 않음 (비우면 제외 없음). 예: ["휠체어", "시야방해"]
# 둘 다 지정하면 포함 조건을 먼저 만족한 뒤 제외 조건에 걸리는 것을 뺀다.
INCLUDE_TEXTS = ["1루", "중앙", "익사이팅"]
EXCLUDE_TEXTS = ["휠체어", "시야방해"]
# 우선순위: 조건에 맞는 후보가 여러 개일 때 먼저 누를 순서. 앞에 쓴 글자일수록 우선이다.
#  - 후보는 포함/제외/SEAT_COUNT 조건을 이미 통과한 등급만이고, 우선순위는 그중에서 고르는 데만 쓰인다.
#  - 이름에 여러 글자가 들어 있으면 가장 앞 순번을 따른다. 어느 글자에도 안 걸리면 맨 뒤.
#  - 같은 순위끼리는 목록 위쪽이 먼저. 비우면 전부 목록 위쪽부터. 예: ["익사이팅", "중앙", "1루"]
PRIORITY_TEXTS = []
REMAIN_COUNT = '[class*="contentRemainCount"]'  # 등급 버튼 안의 잔여수 글자
# 잔여수가 1 이상인 좌석 중 포함/제외 조건에 맞는 것 (SEAT_COUNT 이상인지는 코드에서 숫자로 따로 확인)
SEAT_AVAILABLE = (
    SEAT_ITEM
    + (":is(" + ", ".join(f':has-text("{t}")' for t in INCLUDE_TEXTS) + ")" if INCLUDE_TEXTS else "")
    + "".join(f':not(:has-text("{t}"))' for t in EXCLUDE_TEXTS)
    + f':has({REMAIN_COUNT}:text-matches("^[1-9]"))'
)

# ---- 예매 수량 ----
# 필요한 좌석 수. 두 곳에 쓰인다.
#  1) 감지: 등급의 잔여수가 SEAT_COUNT 이상일 때만 잡는다 (예: 2이면 잔여 1은 무시, 2 이상에서 감지)
#  2) 클릭: 수량 + 버튼을 SEAT_COUNT번 누른다 (수량은 0에서 시작하므로 누른 횟수 = 수량)
# 사이트의 1회 최대 수량을 넘기면 + 버튼이 비활성화되어 해당 단계가 실패한다.
SEAT_COUNT = 1
assert SEAT_COUNT >= 1, "SEAT_COUNT는 1 이상이어야 한다"

# ---- 좌석 클릭 후 순서대로 누를 버튼: (css selector, 글자 정규식 or None) ----
STEPS = [
    ("button", r"자동\s*배정"),                                    # 1) 자동 배정받기
    *[(".nds-e-stepper__incrementButton", None)] * SEAT_COUNT,    # 2) 수량 + (SEAT_COUNT회, 0 -> N)
    ("button", r"선택\s*완료"),                                    # 3) 선택 완료 (데스크톱 xxlarge / 시트 large 둘 다)
]

# ---- 실패 진단 ----
DIAG_SELECTOR = 'button, [role="button"], a'  # 실패 시 화면에서 기록할 요소
DIAG_EXCLUDE_CLASS = "SportsSeatGradeList"    # 좌석 목록 버튼은 기록에서 제외


# ============================================================================
# 로그
# ============================================================================
class Logger:
    """콘솔과 파일에 같은 줄을 남긴다. 시각은 밀리초까지."""

    # 로그 파일 경로를 저장
    def __init__(self, path: Path):
        self.path = path

    # log(...)로 호출하면 한 줄을 시각과 함께 콘솔과 파일에 기록
    def __call__(self, *args):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{ts}] " + " ".join(str(a) for a in args)
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


log = Logger(LOG_FILE)


# ============================================================================
# 좌석 목록
# ============================================================================
class SeatList:
    """탭별 좌석 목록 보임 여부를 기억하고, 조건에 맞는 잔여석이 있으면 우선순위대로 클릭한다."""

    # 탭별 좌석 목록 보임 여부 저장소, 직전에 기록한 '수량 부족' 목록(같은 내용 반복 로그 방지)
    def __init__(self):
        self.on_list: dict[Page, bool] = {}
        self.skipped: str | None = None

    def update_state(self, page: Page) -> bool:
        """보임 여부가 바뀔 때만 FOUND/LOST 로그. 바뀌었으면 True."""
        items = page.locator(SEAT_ITEM).count()
        visible = items > 0
        if visible == self.on_list.get(page, False):
            return False
        self.on_list[page] = visible
        log(f"SEAT LIST {'FOUND' if visible else 'LOST'} ({items} items) | tab: {page.url}")
        return True

    def mark_lost(self, page: Page):
        """목록 보이던 탭이 이동/종료됨 (예: 10분 타임아웃으로 튕김)."""
        if self.on_list.get(page):
            self.on_list[page] = False
            log("SEAT LIST LOST (page navigated/closed)")

    @staticmethod
    def _priority_rank(text: str) -> int:
        """PRIORITY_TEXTS에서 처음 걸리는 글자의 순번(작을수록 우선). 아무것도 안 걸리면 맨 뒤 순위."""
        low = text.lower()
        return next((j for j, t in enumerate(PRIORITY_TEXTS) if t.lower() in low), len(PRIORITY_TEXTS))

    def click_available(self, page: Page) -> str | None:
        """잔여수가 SEAT_COUNT 이상인 후보 중 우선순위가 가장 높은 등급을 클릭하고 '등급명 | 잔여수'를 반환. 없으면 None."""
        candidates = page.locator(SEAT_AVAILABLE)  # 잔여 1 이상 + 포함/제외 조건
        if candidates.count() == 0:  # 대기 없이 즉시 확인
            self.skipped = None
            return None
        # 후보 전부의 [전체 글자, 잔여수 글자]를 한 번에 읽는다
        rows = candidates.evaluate_all(
            "(els, sel) => els.map(e => [e.innerText, (e.querySelector(sel) || {}).innerText || ''])", REMAIN_COUNT)
        ok, short = [], []  # SEAT_COUNT 이상인 후보 / 모자란 후보
        for i, (text, remain_text) in enumerate(rows):
            remain = int(re.sub(r"\D", "", remain_text) or 0)  # "1,605" -> 1605
            name = text.replace("\n", " | ")
            (ok if remain >= SEAT_COUNT else short).append((self._priority_rank(text), i, name))
        if ok:
            rank, i, name = min(ok)  # 우선순위가 높은 것, 같으면 목록 위쪽
            candidates.nth(i).click(force=True, timeout=SEAT_CLICK_TIMEOUT_MS)
            why = f" | priority #{rank + 1} '{PRIORITY_TEXTS[rank]}'" if rank < len(PRIORITY_TEXTS) else ""
            log("SEAT CLICKED:", name + why, "| tab:", page.url)
            return name
        summary = " ; ".join(name for _, _, name in short)
        if summary != self.skipped:  # 같은 내용은 한 번만 기록
            log(f"SEAT SKIPPED (remain < SEAT_COUNT={SEAT_COUNT}):", summary)
            self.skipped = summary
        return None


# ============================================================================
# 좌석 클릭 후 단계
# ============================================================================
class StepRunner:
    """STEPS를 순서대로 클릭한다. 실패하면 로그를 남기고 멈춘다 (좌석을 다시 누르지 않도록)."""

    # css selector와 글자 정규식으로 버튼 locator를 만든다
    @staticmethod
    def _locator(page: Page, sel: str, pat: str | None):
        loc = page.locator(sel)
        return loc.filter(has_text=re.compile(pat)) if pat else loc

    # 단계를 순서대로 클릭하고 단계별/누적 시간을 로그로 남김
    def run(self, page: Page):
        t_seat = time.perf_counter()
        for n, (sel, pat) in enumerate(STEPS, 1):
            t0 = time.perf_counter()
            try:  # 일반 click: 보이고 활성화될 때까지 대기
                self._locator(page, sel, pat).first.click(timeout=STEP_TIMEOUT_MS)
            except Error as e:
                self._log_failure(page, n, sel, pat, e)
                return
            log(f"STEP {n}/{len(STEPS)} OK: {(time.perf_counter() - t0) * 1000:.0f}ms step, "
                f"{(time.perf_counter() - t_seat) * 1000:.0f}ms since seat click | {sel} {pat or ''}")

    @staticmethod
    def _log_failure(page: Page, n: int, sel: str, pat: str | None, err: Exception):
        """실패한 단계와 그때 화면의 버튼 목록, 스크린샷을 남긴다."""
        try:
            buttons = page.eval_on_selector_all(
                DIAG_SELECTOR,
                """(els, exclude) => els.filter(e => !e.className.toString().includes(exclude))
                    .map(e => (e.disabled ? '[disabled] ' : '') + e.tagName + ' ' + e.className + ' | ' + e.innerText.trim().slice(0, 20))""",
                DIAG_EXCLUDE_CLASS)
        except Error as e:
            buttons = f"(n/a: {str(e).splitlines()[0]})"
        log(f"STEP {n}/{len(STEPS)} FAILED\n  selector: {sel} text: {pat}\n  tab: {page.url}\n"
            f"  error: {err}\n  buttons on screen: {buttons}")
        try:
            page.screenshot(path=str(SCREENSHOT_FILE))
        except Error:
            pass


# ============================================================================
# 감시
# ============================================================================
class Watcher:
    """모든 탭을 돌면서 잔여석을 감시한다. 잡으면 단계를 실행하고 브라우저를 열어둔 채 대기."""

    # 브라우저 컨텍스트와 하위 객체(좌석 목록, 단계 실행기) 준비
    def __init__(self, ctx):
        self.ctx = ctx
        self.seats = SeatList()
        self.steps = StepRunner()
        self.last_retry: str | None = None  # 같은 에러 메시지는 연속으로 한 번만 기록

    # 열린 모든 탭을 POLL_SEC 간격으로 무한 순회
    def run(self):
        log("START watching all tabs for available seats | tabs:", [pg.url for pg in self.ctx.pages])
        while True:
            for page in self.ctx.pages:
                try:
                    self._check(page)
                except Error as e:
                    self._on_error(page, e)
            time.sleep(POLL_SEC)

    # 한 탭 검사: 잔여석이 있으면 클릭하고 이어서 단계 실행
    def _check(self, page: Page):
        if self.seats.update_state(page):
            self.last_retry = None  # 복구 후 같은 에러가 또 나면 다시 기록
        if self.seats.click_available(page) is None:
            return
        page.bring_to_front()
        self.steps.run(page)
        log("DONE. holding browser (Ctrl+C to quit)")
        page.wait_for_timeout(10**9)

    def _on_error(self, page: Page, err: Error):
        """이동 중/닫힌 탭 등의 에러 기록."""
        self.seats.mark_lost(page)
        msg = str(err).splitlines()[0]
        if msg != self.last_retry:
            log("RETRY:", msg)
        self.last_retry = msg


# ============================================================================
# Chrome에 접속해서 감시 시작
def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)  # 일반 Chrome에 붙음 (구글 로그인 가능)
        Watcher(browser.contexts[0]).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("STOPPED by Ctrl+C")
    except Exception:
        log("CRASHED\n" + traceback.format_exc())  # 예상 못 한 에러도 로그에 남김
        raise
