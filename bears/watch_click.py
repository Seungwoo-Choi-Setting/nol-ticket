"""NOL 좌석 감시: 잔여수가 SEAT_COUNT_MIN 이상인 등급이 생기면 좌석 등급 -> 자동 배정받기 -> 수량 + (min(잔여수, SEAT_COUNT_MAX)회) -> 선택 완료 순서로 클릭.

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

from playwright.sync_api import Error, Page, expect, sync_playwright

# ============================================================================
# 설정 (바꿀 값은 전부 여기)
# ============================================================================
# ---- 경로 ----
BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "watch.log"        # 실행할 때마다 이어서 쌓임
SCREENSHOT_FILE = BASE_DIR / "diag.png"  # 단계 실패 시 화면 저장

# ---- 브라우저 / 타이밍 ----
CDP_URL = "http://localhost:9222"  # run.ps1이 디버그 포트로 띄운 Chrome
POLL_SEC = 0.1                     # 좌석 목록 확인 간격
SEAT_CLICK_TIMEOUT_MS = 3000       # 좌석 등급 클릭 한도
STEP_TIMEOUT_MS = 10_000           # 클릭 후 단계별 버튼 대기 한도

# ---- 좌석 목록 선택자 ----
SEAT_ITEM = 'button[class*="SportsSeatGradeList_contentItem__"]'
# 포함: 등급 이름에 이 글자 중 하나라도 들어간 것만 누름 (비우면 모든 등급이 대상). 예: ["외야", "네이비"]
# 제외: 등급 이름에 이 글자 중 하나라도 들어가면 누르지 않음 (비우면 제외 없음). 예: ["휠체어", "시야방해"]
# 둘 다 지정하면 포함 조건을 먼저 만족한 뒤 제외 조건에 걸리는 것을 뺀다.
# INCLUDE_TEXTS = ["3루", "중앙"]
INCLUDE_TEXTS = []
EXCLUDE_TEXTS = ["휠체어"]
# 우선순위: 조건에 맞는 후보가 여러 개일 때 먼저 누를 순서. 앞에 쓴 글자일수록 우선이다.
#  - 후보는 포함/제외/SEAT_COUNT_MIN 조건을 이미 통과한 등급만이고, 우선순위는 그중에서 고르는 데만 쓰인다.
#  - 이름에 여러 글자가 들어 있으면 가장 앞 순번을 따른다. 어느 글자에도 안 걸리면 맨 뒤.
#  - 같은 순위끼리는 목록 위쪽이 먼저. 비우면 전부 목록 위쪽부터. 예: ["익사이팅", "중앙", "1루"]
PRIORITY_TEXTS = []
REMAIN_COUNT = '[class*="contentRemainCount"]'  # 등급 버튼 안의 잔여수 글자
# 잔여수가 1 이상인 좌석 중 포함/제외 조건에 맞는 것 (SEAT_COUNT_MIN 이상인지는 코드에서 숫자로 따로 확인)
SEAT_AVAILABLE = (
    SEAT_ITEM
    + (":is(" + ", ".join(f':has-text("{t}")' for t in INCLUDE_TEXTS) + ")" if INCLUDE_TEXTS else "")
    + "".join(f':not(:has-text("{t}"))' for t in EXCLUDE_TEXTS)
    + f':has({REMAIN_COUNT}:text-matches("^[1-9]"))'
)

# ---- 예매 수량 ----
# 잡은 등급의 잔여수에 따라 예매 수량이 정해진다: 수량 = min(잔여수, SEAT_COUNT_MAX)
#  - SEAT_COUNT_MIN: 잔여수가 이 값 미만이면 잡지 않는다 (예: 2이면 잔여 1은 무시)
#  - SEAT_COUNT_MAX: 잔여가 이보다 많아도 이 수만큼만 예매한다
#  예) MIN=1, MAX=2 -> 잔여 1이면 1자리, 잔여 2 이상이면 2자리
#  예) MIN=2, MAX=2 -> 잔여 2 이상일 때만 2자리 (잔여 1은 무시)
#  예) MIN=MAX=N    -> 항상 N자리, 잔여 N 이상일 때만 잡음
# 수량은 0에서 시작해서 + 버튼을 누른 횟수만큼 된다. 사이트의 1회 최대 수량을 넘기면 + 버튼이 비활성화되어 해당 단계가 실패한다.
SEAT_COUNT_MIN = 2
SEAT_COUNT_MAX = 2
assert 1 <= SEAT_COUNT_MIN <= SEAT_COUNT_MAX, "1 <= SEAT_COUNT_MIN <= SEAT_COUNT_MAX 여야 한다"

# ---- 좌석 클릭 후 순서대로 누를 버튼: (css selector, 글자 정규식 or None) ----
STEPS_BEFORE = [("button", r"자동\s*배정")]                  # 1) 자동 배정받기
STEP_INCREMENT = (".nds-e-stepper__incrementButton", None)  # 2) 수량 + (수량만큼 반복, 0 -> N)
STEPS_AFTER = [("button", r"선택\s*완료")]                   # 3) 선택 완료 (데스크톱 xxlarge / 시트 large 둘 다)

# ---- '남은시간 mm:ss' 버튼: 좌석보다 먼저 처리. 비활성(남은시간 글자)이 보인 뒤 활성화되면 바로 클릭 ----
# 클래스 해시(_2p3w6_)는 배포마다 바뀔 수 있어 접두사만 쓴다. 활성화되면 글자가 바뀔 수 있어 활성 쪽은 글자를 안 본다.
NEXT_BUTTON = 'button[class*="_primary_"]:not([disabled])'
# 비활성 '남은시간' 버튼이 있으면 'wait', 활성 primary 버튼이 있으면 'ready', 없으면 null (왕복 1번)
NEXT_STATE_JS = """() => {
    const b = [...document.querySelectorAll('button[class*="_primary_"]')];
    if (b.some(x => x.disabled && x.innerText.includes('남은시간'))) return 'wait';
    return b.some(x => !x.disabled) ? 'ready' : null;
}"""

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

    def click_available(self, page: Page) -> int | None:
        """잔여수가 SEAT_COUNT_MIN 이상인 후보 중 우선순위가 가장 높은 등급을 클릭하고 예매할 수량을 반환. 없으면 None."""
        candidates = page.locator(SEAT_AVAILABLE)  # 잔여 1 이상 + 포함/제외 조건
        t_read = time.perf_counter()
        # 후보 전부의 [전체 글자, 잔여수 글자]를 한 번에 읽는다 (후보 없으면 빈 목록: count 왕복 생략)
        rows = candidates.evaluate_all(
            "(els, sel) => els.map(e => [e.innerText, (e.querySelector(sel) || {}).innerText || ''])", REMAIN_COUNT)
        if not rows:
            self.skipped = None
            return None
        read_ms = (time.perf_counter() - t_read) * 1000
        ok, short = [], []  # SEAT_COUNT_MIN 이상인 후보 / 모자란 후보: (우선순위, 목록 순번, 이름, 잔여수)
        for i, (text, remain_text) in enumerate(rows):
            remain = int(re.sub(r"\D", "", remain_text) or 0)  # "1,605" -> 1605
            name = text.replace("\n", " | ")
            (ok if remain >= SEAT_COUNT_MIN else short).append((self._priority_rank(text), i, name, remain))
        if ok:
            rank, i, name, remain = min(ok)  # 우선순위가 높은 것, 같으면 목록 위쪽
            qty = min(remain, SEAT_COUNT_MAX)  # 잔여가 적으면 그만큼만, 많으면 MAX까지
            t_click = time.perf_counter()
            candidates.nth(i).click(force=True, no_wait_after=True, timeout=SEAT_CLICK_TIMEOUT_MS)
            click_ms = (time.perf_counter() - t_click) * 1000
            why = f" | priority #{rank + 1} '{PRIORITY_TEXTS[rank]}'" if rank < len(PRIORITY_TEXTS) else ""
            log("SEAT CLICKED:", name + why, f"| qty {qty}", f"| read {read_ms:.0f}ms click {click_ms:.0f}ms", "| tab:", page.url)
            return qty
        summary = " ; ".join(name for _, _, name, _ in short)
        if summary != self.skipped:  # 같은 내용은 한 번만 기록
            log(f"SEAT SKIPPED (remain < SEAT_COUNT_MIN={SEAT_COUNT_MIN}):", summary)
            self.skipped = summary
        return None


# ============================================================================
# 좌석 클릭 후 단계
# ============================================================================
class StepRunner:
    """수량에 맞춘 단계(자동 배정받기 -> 수량 + x N -> 선택 완료)를 클릭한다. 실패하면 로그를 남기고 멈춘다 (좌석을 다시 누르지 않도록)."""

    # css selector와 글자 정규식으로 버튼 locator를 만든다
    @staticmethod
    def _locator(page: Page, sel: str, pat: str | None):
        loc = page.locator(sel)
        return loc.filter(has_text=re.compile(pat)) if pat else loc

    # 수량 qty에 맞는 전체 단계 목록: 앞 단계 + 수량 + (qty회) + 뒤 단계
    @staticmethod
    def steps_for(qty: int) -> list[tuple[str, str | None]]:
        return [*STEPS_BEFORE, *[STEP_INCREMENT] * qty, *STEPS_AFTER]

    # 단계를 순서대로 클릭하고 단계별/누적 시간을 로그로 남김
    def run(self, page: Page, qty: int):
        steps = self.steps_for(qty)
        last = len(steps)
        t_seat = time.perf_counter()
        for n, (sel, pat) in enumerate(steps, 1):
            t0 = time.perf_counter()
            try:
                btn = self._locator(page, sel, pat).first
                if n == last:  # 마지막 단계: 활성화만 확인하고 '안정될 때까지 대기'(약 1초)는 건너뜀. 실제 마우스 클릭이라 isTrusted 유지
                    expect(btn).to_be_enabled(timeout=STEP_TIMEOUT_MS)
                    t_clk = time.perf_counter()
                    btn.click(force=True, no_wait_after=True, timeout=STEP_TIMEOUT_MS)
                    log(f"STEP {n} detail: wait {(t_clk - t0) * 1000:.0f}ms click {(time.perf_counter() - t_clk) * 1000:.0f}ms")
                else:  # 일반 click: 보이고 활성화될 때까지 대기. no_wait_after: 클릭 뒤 화면 이동 대기(약 1초)를 건너뜀
                    btn.click(no_wait_after=True, timeout=STEP_TIMEOUT_MS)
            except (Error, AssertionError) as e:
                self._log_failure(page, n, len(steps), sel, pat, e)
                return
            log(f"STEP {n}/{len(steps)} OK: {(time.perf_counter() - t0) * 1000:.0f}ms step, "
                f"{(time.perf_counter() - t_seat) * 1000:.0f}ms since seat click | {sel} {pat or ''}")

    @staticmethod
    def _log_failure(page: Page, n: int, total: int, sel: str, pat: str | None, err: Exception):
        """실패한 단계와 그때 화면의 버튼 목록, 스크린샷을 남긴다."""
        try:
            buttons = page.eval_on_selector_all(
                DIAG_SELECTOR,
                """(els, exclude) => els.filter(e => !e.className.toString().includes(exclude))
                    .map(e => (e.disabled ? '[disabled] ' : '') + e.tagName + ' ' + e.className + ' | ' + e.innerText.trim().slice(0, 20))""",
                DIAG_EXCLUDE_CLASS)
        except Error as e:
            buttons = f"(n/a: {str(e).splitlines()[0]})"
        log(f"STEP {n}/{total} FAILED\n  selector: {sel} text: {pat}\n  tab: {page.url}\n"
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
        self.next_armed: dict[Page, bool] = {}  # 탭별: 비활성 '남은시간' 버튼을 본 적 있음
        self.last_retry: str | None = None  # 같은 에러 메시지는 연속으로 한 번만 기록

    # 남은시간 버튼이 비활성으로 보였다가 활성화되면 클릭 (탭당 1회)
    def _click_next(self, page: Page):
        state = page.evaluate(NEXT_STATE_JS)
        if state == "wait":
            self.next_armed[page] = True
        elif state == "ready" and self.next_armed.get(page):
            self.next_armed[page] = False
            page.locator(NEXT_BUTTON).first.click(force=True, no_wait_after=True, timeout=STEP_TIMEOUT_MS)
            log("NEXT CLICKED | tab:", page.url)

    # 열린 모든 탭을 POLL_SEC 간격으로 무한 순회
    def run(self):
        log("START watching all tabs for available seats | tabs:", [pg.url for pg in self.ctx.pages])
        if self.ctx.pages:  # Python <-> Chrome(CDP) 왕복 시간: 호출 1번당 이만큼은 기본으로 든다
            rtt = []
            for _ in range(10):
                t = time.perf_counter()
                self.ctx.pages[0].evaluate("1")
                rtt.append((time.perf_counter() - t) * 1000)
            log(f"CDP roundtrip: min {min(rtt):.1f}ms avg {sum(rtt) / len(rtt):.1f}ms max {max(rtt):.1f}ms")
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
        self._click_next(page)  # 제일 먼저
        qty = self.seats.click_available(page)
        if qty is None:
            return
        page.bring_to_front()
        self.steps.run(page, qty)
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
