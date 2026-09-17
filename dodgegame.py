#!/usr/bin/env python3
"""터미널 턴제 닷지(turn-based dodge) 게임.

curses 기반 TUI. 한 키 입력 = 한 턴 — 실시간 압박 없이 한 수씩 두며 사방에서
오는 장애물을 피해 오래 버티는 회피 퍼즐이다. 장애물은 화면 가장자리에서
스폰돼 직선으로만 이동하고(호밍 없음), 다음 턴 위치를 흐린 글리프로 미리
보여주는 텔레그래프가 항상 켜져 있다.

게임 상태(DodgeSim)는 curses에 전혀 의존하지 않아 headless self-test로
`--selftest` 옵션을 주면 게임 로직만 순수하게 검증할 수 있다.
표준 라이브러리만 사용하며 유일한 비자명 import는 curses다.
"""

import sys
import time
import random
import signal
import math
from typing import NamedTuple

# curses는 표준 라이브러리이지만 --help/--selftest 경로에서는 굳이 화면을
# 초기화하지 않는다(스펙 요구: selftest는 curses 초기화 없이 순수 로직만 돌림).
# 테마 정의는 curses.COLOR_*/A_BOLD 같은 "그냥 정수 상수"만 참조하므로 import
# 자체는 화면 초기화(initscr)와 무관해 안전하다.
import curses


# ============================================================
# 방향 / 액션
# ============================================================

DIR_UP = (0, -1)
DIR_DOWN = (0, 1)
DIR_LEFT = (-1, 0)
DIR_RIGHT = (1, 0)

# 플레이어 액션 이름 -> (dx, dy). curses 키 매핑과 selftest가 공유하는 어휘.
# 대기(wait) 개념은 없다 — 매 턴 반드시 4방향 중 하나를 골라야 한다(벽 쪽으로
# 누르면 제자리에 남되 턴은 소비되는 것이 사실상 유일한 "머무르기" 수단이다).
ACTIONS = {
    "up": DIR_UP,
    "down": DIR_DOWN,
    "left": DIR_LEFT,
    "right": DIR_RIGHT,
}

# ============================================================
# 스폰 램프 튜닝 상수
# ============================================================

# 생성 확률은 90%에 천천히 가까워지고, 웨이브당 평균 개수는 로그 곡선으로
# 계속 증가한다. 초반 급상승과 40턴 이후의 고정을 함께 없앤다.
SPAWN_CHANCE_BASE = 0.30
SPAWN_CHANCE_LIMIT = 0.90
SPAWN_CHANCE_HALF_TURNS = 80.0      # 시작 확률과 한계 확률의 중간에 도달하는 턴
SPAWN_COUNT_GROWTH_TURNS = 40.0     # 평균 개수 2/3/4/5개: 40/120/280/600턴
SPAWN_MAX_ATTEMPTS = 8              # 즉사 회피 재시도 횟수(초과하면 이번 스폰만 포기)


def spawn_parameters(turn):
    """해당 턴의 웨이브 생성 확률과 웨이브당 평균 시도 개수를 돌려준다.

    개수는 정수로 잘라 단계화하지 않고 _spawn_wave에서 확률적으로 반올림한다.
    평균 2.2개이면 2개를 기본으로, 20% 확률로 1개를 더 시도한다.
    """
    progress = max(0, turn)
    chance = SPAWN_CHANCE_BASE + (SPAWN_CHANCE_LIMIT - SPAWN_CHANCE_BASE) * (
        progress / (progress + SPAWN_CHANCE_HALF_TURNS)
    )
    mean_count = 1.0 + math.log2(1.0 + progress / SPAWN_COUNT_GROWTH_TURNS)
    return chance, mean_count


class Obstacle:
    """직선으로 등속 이동하는 장애물 1개. (x, y) = 현재 칸, (dx, dy) = 매 턴 이동량."""

    __slots__ = ("x", "y", "dx", "dy")

    def __init__(self, x, y, dx, dy):
        self.x = x
        self.y = y
        self.dx = dx
        self.dy = dy

    def next_pos(self):
        """텔레그래프용 — 다음 턴에 이 장애물이 있을 칸."""
        return (self.x + self.dx, self.y + self.dy)


class DodgeSim:
    """턴제 닷지 게임의 순수 로직. curses에 의존하지 않는다.

    RNG는 외부에서 주입받는다(random.Random 인스턴스) — 시드를 고정하면
    스폰 웨이브까지 포함해 완전히 결정적으로 재현된다.
    """

    def __init__(self, width, height, rng=None):
        self.width = width
        self.height = height
        self.rng = rng if rng is not None else random.Random()
        self.reset()

    def reset(self):
        """새 게임 시작 상태로 초기화 — 턴 0, 장애물 없음, 플레이어는 중앙."""
        self.player_x = self.width // 2
        self.player_y = self.height // 2
        self.obstacles = []
        self.turn = 0
        self.game_over = False

    @property
    def score(self):
        """점수 = 생존 턴 수."""
        return self.turn

    def in_bounds(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height

    def step(self, action):
        """턴 1회 진행: 플레이어 이동 -> 장애물 전진 -> 충돌판정 -> 턴+1 -> 스폰.

        게임오버 상태에서는 아무것도 하지 않는다(재시작은 reset() 호출).
        """
        if self.game_over:
            return

        # --- 1) 플레이어가 1칸 이동 ---
        # 4방향 action만 허용한다 — 그 외(예: 과거의 "wait")는 KeyError를 던지고
        # 아무 상태도 바꾸지 않는다(턴 진행 없음). 유효하지 않은 action을 애초에
        # 넘기지 않는 것은 호출측(run_game의 resolve_key)의 책임이다.
        dx, dy = ACTIONS[action]
        player_before = (self.player_x, self.player_y)
        new_x, new_y = self.player_x + dx, self.player_y + dy
        if self.in_bounds(new_x, new_y):
            self.player_x, self.player_y = new_x, new_y
        # 벽(격자 밖)이면 이동이 무효 처리되고 제자리에 남는다 — 그래도 이번
        # 턴 자체는 정상 소비된다(장애물 전진/턴 카운트는 그대로 진행).
        player_after = (self.player_x, self.player_y)

        # --- 2) 모든 장애물이 1스텝 전진 ---
        moved = []
        for obs in self.obstacles:
            before = (obs.x, obs.y)
            obs.x += obs.dx
            obs.y += obs.dy
            moved.append((obs, before, (obs.x, obs.y)))

        # --- 3) 충돌 판정 ---
        # 직접 겹침: 장애물이 플레이어의 새 칸으로 들어옴.
        # pass-through(스쳐 지나가기): 플레이어와 장애물이 서로의 시작 칸으로
        # 동시에 맞바꿔 들어가면 좌표상 안 겹치고 지나쳐 버리므로 따로 잡아야
        # 한다 — before가 player_after, after가 player_before인 경우가 그것.
        collided = False
        for obs, before, after in moved:
            if after == player_after:
                collided = True
            elif before == player_after and after == player_before:
                collided = True
        if collided:
            self.game_over = True

        # --- 4) 격자 밖으로 나간 장애물 제거 ---
        self.obstacles = [o for o, _before, _after in moved if self.in_bounds(o.x, o.y)]

        # --- 5) 턴 +1, 스폰 램프 ---
        self.turn += 1
        if not self.game_over:
            self._spawn_wave()

    # --- 스폰 램프 ---

    def _spawn_wave(self):
        chance, mean_count = spawn_parameters(self.turn)
        if self.rng.random() >= chance:
            return
        count = int(mean_count)
        if self.rng.random() < mean_count - count:
            count += 1
        for _ in range(count):
            obs = self._make_spawn_candidate()
            if obs is not None:
                self.obstacles.append(obs)

    def _make_spawn_candidate(self):
        """가장자리에서 안쪽을 향하는 장애물 하나를 만든다.

        스폰 직후 다음 턴 위치가 곧바로 플레이어 칸이 되는 즉사 조합은
        재시도로 피한다(적어도 텔레그래프 1턴은 보이게).
        """
        player_pos = (self.player_x, self.player_y)
        for _ in range(SPAWN_MAX_ATTEMPTS):
            edge = self.rng.choice(("top", "bottom", "left", "right"))
            if edge == "top":
                x, y = self.rng.randrange(self.width), 0
                dx, dy = self.rng.choice((-1, 0, 1)), 1
            elif edge == "bottom":
                x, y = self.rng.randrange(self.width), self.height - 1
                dx, dy = self.rng.choice((-1, 0, 1)), -1
            elif edge == "left":
                x, y = 0, self.rng.randrange(self.height)
                dx, dy = 1, self.rng.choice((-1, 0, 1))
            else:
                x, y = self.width - 1, self.rng.randrange(self.height)
                dx, dy = -1, self.rng.choice((-1, 0, 1))

            if (x, y) == player_pos:
                continue
            if (x + dx, y + dy) == player_pos:
                continue  # 스폰 즉시 다음 턴에 플레이어를 덮치는 조합 배제
            return Obstacle(x, y, dx, dy)
        return None


# ============================================================
# curses 렌더 / 입력
# ============================================================

MIN_TERM_COLS = 24
MIN_TERM_ROWS = 12

# 이동은 방향키만 받는다 — hjkl도, 대기(wait)도 없다. 매 턴 반드시 4방향 중
# 하나를 골라야 한다.
KEY_TO_ACTION = {
    curses.KEY_UP: "up",
    curses.KEY_DOWN: "down",
    curses.KEY_LEFT: "left",
    curses.KEY_RIGHT: "right",
}

QUIT_KEYS = (ord("q"),)  # 종료는 q만. ESC는 메뉴 안의 취소 전용(메뉴 컨텍스트 한정).
RESTART_KEY = ord("r")
MENU_KEY = ord("/")


class ColorPairCache:
    """전경색별 curses color pair를 on-demand 생성해 캐시한다.

    배경은 항상 터미널 기본값(-1, use_default_colors)을 쓴다 — 이 게임은
    falling_sand처럼 배경을 순검정으로 강제할 이유가 없다(빈 칸은 그냥
    터미널 배경 그대로 노출).
    """

    def __init__(self):
        self.cache = {}
        self.next_id = 1
        self.has_color = curses.has_colors()
        self.max_pairs = max(curses.COLOR_PAIRS, 1) if self.has_color else 1
        # curses.COLORS는 start_color() 이후에만 유효하다 -- 이 클래스는 항상
        # run_game에서 start_color() 다음에 만들어지므로 정상적으로 읽히지만,
        # 혹시 모를 초기화 순서 실수에도 크래시하지 않도록 방어한다. 256색
        # 이상이면 테마의 확장(ext) 색을, 아니면 8색 폴백(fallback8)을 쓴다.
        try:
            total_colors = curses.COLORS
        except AttributeError:
            total_colors = 0
        self.use_extended = self.has_color and total_colors >= 256

    def get_pair(self, fg):
        if not self.has_color:
            return 0
        cached = self.cache.get(fg)
        if cached is not None:
            return cached
        if self.next_id >= self.max_pairs:
            return 0
        pair_id = self.next_id
        try:
            curses.init_pair(pair_id, fg, -1)
        except curses.error:
            return 0
        self.cache[fg] = pair_id
        self.next_id += 1
        return pair_id


def _theme_attr(pairs, color, attr):
    """테마의 (색, 속성)을 curses attr 값 하나로 합성한다.

    color가 None이면 색 없이 속성(굵기/흐림)만 쓴다(Mono 테마, 또는
    무채색 터미널에서의 자연스러운 폴백). color가 ThemeColor면
    pairs.use_extended(런타임에 curses.COLORS>=256인지)에 따라 256색 확장
    인덱스(ext) 또는 8색 폴백(fallback8) 중 맞는 쪽을 골라 쓴다.
    """
    if color is None:
        return attr
    fg = color.ext if pairs.use_extended else color.fallback8
    return curses.color_pair(pairs.get_pair(fg)) | attr


class ThemeColor(NamedTuple):
    """테마 색 필드 하나 -- 256색 확장 인덱스와 8색 폴백의 쌍.

    ext: 터미널이 256색 이상을 지원할 때 쓰는 색 인덱스(0~255).
    fallback8: 8색 전용 터미널에서 쓰는 curses.COLOR_* 상수(0~7 범위).
    기존 5개 테마(Classic/Mono/Neon/Amber/Ocean)는 ext와 fallback8이 같은
    표준 8색 값이라, 8색 터미널에서의 모습이 이번 확장 전과 완전히 동일하다.
    curses 화면 초기화 없이도 만들 수 있다(정수 두 개만 담는 그릇이라서).
    """

    ext: int
    fallback8: int


class Theme(NamedTuple):
    """색 전용 테마 1종. 플레이어/장애물/텔레그래프/테두리/HUD 각각의
    (색, curses 속성)만 담는다 — 글리프는 테마가 아니라 스킨(§작업 A/B)이
    결정하므로 여기 없다.

    color 필드는 ThemeColor(확장색, 8색폴백) 또는 None(무채색, 속성만
    사용)이다. 이 클래스는 curses 화면 초기화 없이도 만들 수 있다(색
    상수는 그냥 정수, ThemeColor는 정수 두 개짜리 순수 데이터).
    """

    name: str
    player_color: object
    player_attr: int
    obstacle_color: object
    obstacle_attr: int
    telegraph_color: object
    telegraph_attr: int
    border_color: object
    border_attr: int
    hud_color: object
    hud_attr: int


def _std(color):
    """표준 8색 curses.COLOR_* 상수를 ext==fallback8인 ThemeColor로 감싼다.

    기존 5개 테마가 이걸 쓴다 -- 8색 터미널에서 index가 그대로 표준 색이므로
    256색 확장 전과 폴백 시 결과가 완전히 동일하다(§작업 A "색 구성 불변").
    """
    return ThemeColor(ext=color, fallback8=color)


# 테마 10종(기존 5 + 신규 5). 인덱스 0(Classic)이 기본값 — 프로세스가
# 끝나면 항상 이 테마로 돌아간다(테마는 저장하지 않는다, 설정 파일 없음).
# 기존 5개(Classic/Mono/Neon/Amber/Ocean)의 색 구성은 그대로다 -- _std()로
# 표준 8색을 ThemeColor로 감쌌을 뿐 실제 값은 안 바뀌었다. 신규 5개는 256색
# 확장 팔레트(ThemeColor(ext=..., fallback8=...))를 써서 10종이 화면에서
# 서로 구분되게 했다(§작업 B selftest가 player/obstacle 조합 유일성을
# 기계로 고정). 8색 전용 터미널에서는 폴백값 특성상 일부 신규 테마가 서로
# 비슷해 보일 수 있다 -- 8색의 물리적 한계이며 의도적으로 허용한다.
THEMES = (
    Theme(
        name="Classic",
        player_color=_std(curses.COLOR_GREEN), player_attr=curses.A_BOLD,
        obstacle_color=_std(curses.COLOR_RED), obstacle_attr=curses.A_BOLD,
        telegraph_color=_std(curses.COLOR_YELLOW), telegraph_attr=curses.A_DIM,
        border_color=_std(curses.COLOR_WHITE), border_attr=curses.A_NORMAL,
        hud_color=_std(curses.COLOR_WHITE), hud_attr=curses.A_BOLD,
    ),
    Theme(
        name="Mono",
        player_color=None, player_attr=curses.A_BOLD,
        obstacle_color=None, obstacle_attr=curses.A_NORMAL,
        telegraph_color=None, telegraph_attr=curses.A_DIM,
        border_color=None, border_attr=curses.A_NORMAL,
        hud_color=None, hud_attr=curses.A_BOLD,
    ),
    Theme(
        name="Neon",
        player_color=_std(curses.COLOR_MAGENTA), player_attr=curses.A_BOLD,
        obstacle_color=_std(curses.COLOR_CYAN), obstacle_attr=curses.A_BOLD,
        telegraph_color=_std(curses.COLOR_CYAN), telegraph_attr=curses.A_DIM,
        border_color=_std(curses.COLOR_MAGENTA), border_attr=curses.A_DIM,
        hud_color=_std(curses.COLOR_MAGENTA), hud_attr=curses.A_BOLD,
    ),
    Theme(
        name="Amber",
        player_color=_std(curses.COLOR_YELLOW), player_attr=curses.A_BOLD,
        obstacle_color=_std(curses.COLOR_YELLOW), obstacle_attr=curses.A_NORMAL,
        telegraph_color=_std(curses.COLOR_YELLOW), telegraph_attr=curses.A_DIM,
        border_color=_std(curses.COLOR_YELLOW), border_attr=curses.A_DIM,
        hud_color=_std(curses.COLOR_YELLOW), hud_attr=curses.A_BOLD,
    ),
    Theme(
        name="Ocean",
        player_color=_std(curses.COLOR_WHITE), player_attr=curses.A_BOLD,
        obstacle_color=_std(curses.COLOR_BLUE), obstacle_attr=curses.A_BOLD,
        telegraph_color=_std(curses.COLOR_CYAN), telegraph_attr=curses.A_DIM,
        border_color=_std(curses.COLOR_CYAN), border_attr=curses.A_NORMAL,
        hud_color=_std(curses.COLOR_WHITE), hud_attr=curses.A_BOLD,
    ),
    Theme(
        # 짙은 숲 -- 밝은 라임그린 플레이어 vs 흙갈색(amber-brown) 적.
        name="Forest",
        player_color=ThemeColor(ext=118, fallback8=curses.COLOR_GREEN), player_attr=curses.A_BOLD,
        obstacle_color=ThemeColor(ext=130, fallback8=curses.COLOR_YELLOW), obstacle_attr=curses.A_BOLD,
        telegraph_color=ThemeColor(ext=22, fallback8=curses.COLOR_GREEN), telegraph_attr=curses.A_DIM,
        border_color=ThemeColor(ext=28, fallback8=curses.COLOR_GREEN), border_attr=curses.A_NORMAL,
        hud_color=ThemeColor(ext=34, fallback8=curses.COLOR_GREEN), hud_attr=curses.A_BOLD,
    ),
    Theme(
        # 노을 -- 주황 플레이어 vs 진분홍(rose) 적.
        name="Sunset",
        player_color=ThemeColor(ext=208, fallback8=curses.COLOR_RED), player_attr=curses.A_BOLD,
        obstacle_color=ThemeColor(ext=198, fallback8=curses.COLOR_MAGENTA), obstacle_attr=curses.A_BOLD,
        telegraph_color=ThemeColor(ext=223, fallback8=curses.COLOR_YELLOW), telegraph_attr=curses.A_DIM,
        border_color=ThemeColor(ext=166, fallback8=curses.COLOR_RED), border_attr=curses.A_NORMAL,
        hud_color=ThemeColor(ext=214, fallback8=curses.COLOR_YELLOW), hud_attr=curses.A_BOLD,
    ),
    Theme(
        # 얼음 -- 하늘색 플레이어 vs 서리 흰색 적.
        name="Ice",
        player_color=ThemeColor(ext=45, fallback8=curses.COLOR_CYAN), player_attr=curses.A_BOLD,
        obstacle_color=ThemeColor(ext=255, fallback8=curses.COLOR_WHITE), obstacle_attr=curses.A_BOLD,
        telegraph_color=ThemeColor(ext=81, fallback8=curses.COLOR_CYAN), telegraph_attr=curses.A_DIM,
        border_color=ThemeColor(ext=39, fallback8=curses.COLOR_BLUE), border_attr=curses.A_NORMAL,
        hud_color=ThemeColor(ext=231, fallback8=curses.COLOR_WHITE), hud_attr=curses.A_BOLD,
    ),
    Theme(
        # 형광 -- 형광 라임 플레이어 vs 전기보라 적. Neon(마젠타/시안)과
        # 안 겹치도록 색상환에서 떨어진 라임/퍼플을 썼다.
        name="Toxic",
        player_color=ThemeColor(ext=154, fallback8=curses.COLOR_GREEN), player_attr=curses.A_BOLD,
        obstacle_color=ThemeColor(ext=129, fallback8=curses.COLOR_MAGENTA), obstacle_attr=curses.A_BOLD,
        telegraph_color=ThemeColor(ext=100, fallback8=curses.COLOR_GREEN), telegraph_attr=curses.A_DIM,
        border_color=ThemeColor(ext=93, fallback8=curses.COLOR_MAGENTA), border_attr=curses.A_NORMAL,
        hud_color=ThemeColor(ext=154, fallback8=curses.COLOR_GREEN), hud_attr=curses.A_BOLD,
    ),
    Theme(
        # 진홍 -- 은회색 플레이어 vs 짙은 진홍색 적.
        name="Crimson",
        player_color=ThemeColor(ext=252, fallback8=curses.COLOR_WHITE), player_attr=curses.A_BOLD,
        obstacle_color=ThemeColor(ext=160, fallback8=curses.COLOR_RED), obstacle_attr=curses.A_BOLD,
        telegraph_color=ThemeColor(ext=88, fallback8=curses.COLOR_RED), telegraph_attr=curses.A_DIM,
        border_color=ThemeColor(ext=124, fallback8=curses.COLOR_RED), border_attr=curses.A_NORMAL,
        hud_color=ThemeColor(ext=196, fallback8=curses.COLOR_RED), hud_attr=curses.A_BOLD,
    ),
)

# 텔레그래프 글리프는 스킨 대상이 아니다 — 항상 이 문자로 고정(적 글리프와
# 시각적으로 구분돼야 하므로).
TELEGRAPH_GLYPH = "·"

# 플레이어/적 스킨 후보 — 전부 단일폭 ASCII. 인덱스 0이 기본값.
PLAYER_SKINS = ("@", "O", "0", "&", "A", "%", "8")
ENEMY_SKINS = ("*", "#", "x", "o", "^", "+", "!")
DEFAULT_PLAYER_GLYPH = PLAYER_SKINS[0]
DEFAULT_ENEMY_GLYPH = ENEMY_SKINS[0]


def _move_menu_selection(index, delta, count):
    """테마 메뉴 커서를 delta칸 이동하되 [0, count) 범위를 순환(wrap)한다.

    0에서 위로 가면 마지막으로, 마지막에서 아래로 가면 0으로 돈다 — 경계에서
    막히지 않고 항상 안전한 인덱스를 반환한다. count<=0(빈 목록)이면 0을
    반환한다(방어). curses에 의존하지 않는 순수 함수라 selftest에서 검증 가능.
    """
    if count <= 0:
        return 0
    return (index + delta) % count


_MENU_CANCEL_KEYS = (27, ord("/"), ord("q"))  # ESC, '/', 'q' — 셋 다 메뉴 취소
_MENU_CONFIRM_KEYS = (10, 13, curses.KEY_ENTER)  # 개행/캐리지리턴/터미널별 KEY_ENTER

# 메뉴 칸 인덱스 — 좌->우 순서로 테마/나(플레이어)/적(장애물).
MENU_COL_THEME = 0
MENU_COL_PLAYER = 1
MENU_COL_ENEMY = 2
MENU_COL_COUNT = 3


class MenuState(NamedTuple):
    """`/` 메뉴(테마/나/적 3칸)의 현재 커서·선택 상태.

    active_col: 지금 조작 중인 칸(MENU_COL_*). theme_idx: 테마 칸 커서 색인.
    player_idx/enemy_idx: 스킨 후보 리스트 안에서의 색인 — 후보에 없는 문자를
    직접 입력했으면 None("직접 입력" 상태). player_char/enemy_char: 실제
    적용될 글리프 문자(직접 입력 여부와 무관하게 항상 최신값을 담는다).
    curses 없이 만들 수 있는 순수 데이터라 selftest에서 검증 가능하다.
    """

    active_col: int
    theme_idx: int
    player_idx: object  # int 또는 None
    player_char: str
    enemy_idx: object  # int 또는 None
    enemy_char: str


def make_menu_state(theme_index, player_char, enemy_char,
                     player_candidates=PLAYER_SKINS, enemy_candidates=ENEMY_SKINS):
    """메뉴를 여는 시점의 초기 MenuState를 만든다.

    현재 확정된 테마/플레이어글리프/적글리프를 그대로 커서 위치로 삼는다
    (메뉴를 열자마자 아무것도 안 바꾸고 Enter를 눌러도 기존 설정이 그대로
    유지되도록). 글리프가 후보 리스트에 없으면(과거 직접 입력값) idx=None.
    """
    player_idx = player_candidates.index(player_char) if player_char in player_candidates else None
    enemy_idx = enemy_candidates.index(enemy_char) if enemy_char in enemy_candidates else None
    return MenuState(
        active_col=MENU_COL_THEME,
        theme_idx=theme_index,
        player_idx=player_idx,
        player_char=player_char,
        enemy_idx=enemy_idx,
        enemy_char=enemy_char,
    )


def _move_skin_index(idx, delta, count):
    """스킨 후보 리스트 안에서 커서를 delta칸 이동한다(순환).

    idx가 None("직접 입력" 상태에서 위/아래를 눌렀을 때)이면 아래(delta>0)는
    첫 후보(0)로, 위(delta<0)는 마지막 후보로 진입한다 — 어느 방향이든 항상
    유효한 색인에 안착한다. count<=0이면 0을 반환(방어).
    """
    if count <= 0:
        return 0
    if idx is None:
        return 0 if delta > 0 else count - 1
    return _move_menu_selection(idx, delta, count)


def _printable_ascii_char(key):
    """key가 인쇄 가능 ASCII 1글자(`!`~`~`, 33~126)이면 그 문자를, 아니면 None을
    반환한다. 방향키·Enter·ESC 같은 curses 특수키·제어문자는 이 범위 밖이라
    자동으로 배제된다(별도 예외 목록이 필요 없다).
    """
    if 33 <= key <= 126:
        return chr(key)
    return None


def resolve_menu_key(key, state, theme_count,
                      player_candidates=PLAYER_SKINS, enemy_candidates=ENEMY_SKINS):
    """3칸(테마/나/적) 메뉴가 열려있는 동안의 키 입력 1건을 해석한다.

    반환: (new_state, command). command는 None(커서/칸 이동 또는 무시) /
    "confirm"(선택 확정) / "cancel"(메뉴 닫고 원래 설정 유지) 중 하나.
    ←→는 칸 이동(3칸 순환), ↑↓는 활성 칸 안에서 후보 이동(테마 칸은 테마
    색인, 나/적 칸은 스킨 후보 색인 — 직접 입력 상태에서도 안전 진입).
    나/적 칸이 활성일 때 인쇄 가능 ASCII 문자 키는 후보에 없어도 즉시 그 칸의
    글리프가 된다("직접 입력"). curses 화면에 의존하지 않아 selftest에서
    순수하게 검증 가능하다.
    """
    if key in _MENU_CANCEL_KEYS:
        return state, "cancel"
    if key in _MENU_CONFIRM_KEYS:
        return state, "confirm"
    if key == curses.KEY_LEFT:
        return state._replace(active_col=_move_menu_selection(state.active_col, -1, MENU_COL_COUNT)), None
    if key == curses.KEY_RIGHT:
        return state._replace(active_col=_move_menu_selection(state.active_col, 1, MENU_COL_COUNT)), None
    if key in (curses.KEY_UP, curses.KEY_DOWN):
        delta = -1 if key == curses.KEY_UP else 1
        if state.active_col == MENU_COL_THEME:
            return state._replace(theme_idx=_move_menu_selection(state.theme_idx, delta, theme_count)), None
        if state.active_col == MENU_COL_PLAYER:
            new_idx = _move_skin_index(state.player_idx, delta, len(player_candidates))
            return state._replace(player_idx=new_idx, player_char=player_candidates[new_idx]), None
        new_idx = _move_skin_index(state.enemy_idx, delta, len(enemy_candidates))
        return state._replace(enemy_idx=new_idx, enemy_char=enemy_candidates[new_idx]), None
    if state.active_col in (MENU_COL_PLAYER, MENU_COL_ENEMY):
        ch = _printable_ascii_char(key)
        if ch is not None:
            if state.active_col == MENU_COL_PLAYER:
                idx = player_candidates.index(ch) if ch in player_candidates else None
                return state._replace(player_idx=idx, player_char=ch), None
            idx = enemy_candidates.index(ch) if ch in enemy_candidates else None
            return state._replace(enemy_idx=idx, enemy_char=ch), None
    return state, None


def _skin_column_lines(candidates, selected_idx, current_char):
    """스킨 칸(나/적) 한 칸의 표시줄들을 만든다 — 후보 목록 + "직접 입력" 줄.

    렌더링과 분리된 순수 함수(문자열 리스트만 반환) — curses 없이도 만들 수
    있다. "직접:X" 줄은 selected_idx가 None(후보에 없는 문자를 직접 입력한
    상태)일 때만 커서 마커(>)가 붙는다.
    """
    lines = []
    for i, ch in enumerate(candidates):
        marker = ">" if i == selected_idx else " "
        lines.append(f"{marker} {ch}")
    custom_marker = ">" if selected_idx is None else " "
    lines.append(f"{custom_marker} Custom:{current_char}")
    return lines


def _active_row_index(state, player_candidates=PLAYER_SKINS, enemy_candidates=ENEMY_SKINS):
    """지금 활성 칸(state.active_col)의 커서가 메뉴 행(row) 목록에서 몇 번째
    행에 있는지 반환한다. 3칸(테마/나/적)이 한 줄에 나란히 그려지므로
    "칸 안 커서 색인"이 아니라 "합쳐진 행 목록 안 색인"이 있어야 스크롤
    계산(compute_scroll_window)에 넘길 수 있다. 나/적 칸이 "직접 입력"
    (idx=None) 상태면 후보 목록 바로 다음의 Custom 줄을 가리킨다. curses에
    의존하지 않는 순수 함수 — draw_skin_menu와 selftest가 공유한다.
    """
    if state.active_col == MENU_COL_THEME:
        return state.theme_idx
    if state.active_col == MENU_COL_PLAYER:
        return state.player_idx if state.player_idx is not None else len(player_candidates)
    return state.enemy_idx if state.enemy_idx is not None else len(enemy_candidates)


def compute_scroll_window(total_rows, visible_rows, cursor_row):
    """스크롤 가능한 행 목록에서 cursor_row가 항상 보이는 창 [start, end)을
    계산한다(작업 C). curses에 의존하지 않는 순수 함수라 selftest에서
    직접 검증할 수 있다.

    - total_rows가 visible_rows 이하면 스크롤이 필요 없다 — 그대로
      (0, total_rows)를 반환한다(목록이 창보다 짧으면 스크롤 안 일어남).
    - 그 외엔 cursor_row가 창 중앙 근처에 오도록 하되, 창이 목록의
      처음/끝 경계를 넘지 않게 clamp한다 — 그래서 항상 꽉 찬 창을
      반환하고(clamp 전에는 중앙 배치, clamp 후에도 폭은 유지), cursor_row는
      반환된 [start, end) 안에 반드시 포함된다.
    - visible_rows<=0이면 빈 창 (0, 0)을 반환한다(방어).
    """
    if visible_rows <= 0:
        return 0, 0
    if total_rows <= visible_rows:
        return 0, total_rows
    half = visible_rows // 2
    start = cursor_row - half
    start = max(0, min(start, total_rows - visible_rows))
    return start, start + visible_rows


def draw_skin_menu(stdscr, state, themes, max_y, max_x,
                    player_candidates=PLAYER_SKINS, enemy_candidates=ENEMY_SKINS):
    """테마/나/적 3칸 메뉴를 화면 중앙에 작은 박스로 그린다.

    각 칸을 세로로 정렬된 텍스트 열로 만든 뒤 하나의 박스 텍스트 블록으로
    합친다(기존 draw_theme_menu와 동일한 클램프+try/except 방어 패턴을
    재사용) — 박스가 터미널보다 크면 조용히 잘라내거나(폭 부족) 아예
    그리기를 포기한다(4x3 미만), 크래시하지 않는다.

    테마가 10종이라 헤더+테두리+힌트까지 합치면 작은 터미널에서 박스가
    화면보다 커질 수 있다(§작업 C). 그때는 헤더/힌트 줄은 고정한 채 행
    (row) 영역만 compute_scroll_window로 스크롤해 활성 칸의 커서가 항상
    보이게 하고, 위/아래가 잘렸으면 마커 줄(`^`/`v`)로 알린다. 마커 줄도
    화면 한 줄을 차지하므로, 마커가 필요해지면 그만큼 예산을 줄여 커서
    가시성을 다시 계산한다(고정점 반복 — 마커 개수는 0/1/2 셋 중 하나뿐이라
    금방 수렴한다).
    """
    headers = ("Theme", "You", "Enemy")
    theme_lines = []
    for i, t in enumerate(themes):
        marker = ">" if i == state.theme_idx else " "
        theme_lines.append(f"{marker} {t.name}")
    player_lines = _skin_column_lines(player_candidates, state.player_idx, state.player_char)
    enemy_lines = _skin_column_lines(enemy_candidates, state.enemy_idx, state.enemy_char)
    columns = (theme_lines, player_lines, enemy_lines)

    col_widths = [max([len(h)] + [len(s) for s in col]) for h, col in zip(headers, columns)]
    header_cells = []
    for i, h in enumerate(headers):
        text = f"[{h}]" if i == state.active_col else f" {h} "
        header_cells.append(text.center(col_widths[i] + 2))
    n_rows = max(len(c) for c in columns)
    row_texts = []
    for r in range(n_rows):
        cells = []
        for i, col in enumerate(columns):
            cell = col[r] if r < len(col) else ""
            cells.append(f" {cell.ljust(col_widths[i])} ")
        row_texts.append("|".join(cells))
    header_row = "|".join(header_cells)
    footer = " <-> Move   ^v Select   Enter OK   ESC/q Cancel   (You/Enemy: press a key) "

    # 폭은 스크롤 여부와 무관하게 전체 행 기준으로 계산한다 — 스크롤 중에
    # 박스 너비가 흔들리면 보기 나쁘므로, 보이는 슬라이스가 아니라 항상
    # 전체 row_texts로 폭을 정한다.
    width_probe = [header_row, footer] + row_texts
    box_w = min(max_x - 2, max(len(s) for s in width_probe) + 4)
    if box_w < 4:
        return

    # 높이 예산: 테두리(위/아래 2줄) + 헤더(1줄) + 힌트(1줄) = 고정 4줄을
    # 뺀 나머지가 행에 쓸 수 있는 예산이다. cursor_row가 그 예산 안에서
    # 항상 보이도록 compute_scroll_window를 쓰고, 잘린 쪽이 있으면 마커
    # 한 줄만큼 예산을 다시 빼 재계산한다(고정점에 수렴할 때까지).
    available_box_h = max(0, max_y - 2)
    row_budget = max(0, available_box_h - 4)
    total_rows = len(row_texts)
    cursor_row = _active_row_index(state, player_candidates, enemy_candidates)

    marker_cost = 0
    start, end = compute_scroll_window(total_rows, row_budget, cursor_row)
    top_clipped = start > 0
    bottom_clipped = end < total_rows
    for _ in range(3):  # 마커 개수는 {0,1,2}뿐이라 3회면 항상 수렴한다
        new_cost = (1 if top_clipped else 0) + (1 if bottom_clipped else 0)
        if new_cost == marker_cost:
            break
        marker_cost = new_cost
        start, end = compute_scroll_window(total_rows, max(0, row_budget - marker_cost), cursor_row)
        top_clipped = start > 0
        bottom_clipped = end < total_rows

    display_rows = []
    if top_clipped:
        display_rows.append(f"   ^ {start} more above ^")
    display_rows.extend(row_texts[start:end])
    if bottom_clipped:
        display_rows.append(f"   v {total_rows - end} more below v")

    lines = [header_row] + display_rows + [footer]
    box_h = min(available_box_h, len(lines) + 2)
    if box_h < 3:
        return

    top = max(0, (max_y - box_h) // 2)
    left = max(0, (max_x - box_w) // 2)
    attr = curses.A_BOLD | curses.A_REVERSE

    try:
        for r in range(box_h):
            y = top + r
            if r == 0 or r == box_h - 1:
                stdscr.addstr(y, left, "+" + "-" * (box_w - 2) + "+", attr)
            else:
                text = lines[r - 1] if r - 1 < len(lines) else ""
                padded = (" " + text).ljust(box_w - 2)[: box_w - 2]
                stdscr.addstr(y, left, "|" + padded + "|", attr)
    except curses.error:
        pass


def resolve_key(key):
    """키 입력 1건을 (action, command)로 변환한다.

    action은 DodgeSim.step()에 그대로 넘기는 문자열, command는 "quit"/
    "restart"/None. 둘 다 None이면 이번 키는 게임에 영향 없음(다음 프레임
    다시 렌더만 함).
    """
    if key in QUIT_KEYS:
        return None, "quit"
    if key == RESTART_KEY:
        return None, "restart"
    action = KEY_TO_ACTION.get(key)
    if action is not None:
        return action, None
    return None, None


def _too_small(max_y, max_x):
    return max_x < MIN_TERM_COLS or max_y < MIN_TERM_ROWS


def _wait_for_bigger_terminal(stdscr):
    """터미널이 너무 작으면 크래시 대신 안내 메시지를 띄우고 대기한다."""
    stdscr.nodelay(True)
    while True:
        max_y, max_x = stdscr.getmaxyx()
        if not _too_small(max_y, max_x):
            stdscr.nodelay(False)
            return
        stdscr.erase()
        msg = f"Please make the terminal bigger (min {MIN_TERM_COLS}x{MIN_TERM_ROWS}, now {max_x}x{max_y})"
        try:
            stdscr.addstr(0, 0, msg[: max(0, max_x - 1)])
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.getch()
        if key in QUIT_KEYS:
            raise SystemExit(0)
        time.sleep(0.05)


def _make_sim_for_screen(max_y, max_x, rng):
    """터미널 크기에 맞춰 DodgeSim을 만든다.

    상하좌우 테두리 각 1칸 + 하단 HUD 1줄을 뺀 나머지가 플레이 가능 격자다.
    """
    width = max(5, max_x - 2)
    height = max(5, max_y - 3)
    return DodgeSim(width, height, rng=rng)


def draw_hud(stdscr, row, max_x, sim, pairs, theme):
    # turn과 score는 항상 같은 값(score는 turn의 property alias)이라 화면에는
    # 점수 하나만 보여준다 — 내부 sim.turn 필드/score property 자체는 로직·
    # selftest가 쓰므로 그대로 둔다(표시만 하나로 합침).
    if sim.game_over:
        text = f" GAME OVER   Score: {sim.score}   [r] Restart   [/] Menu   [q] Quit"
        attr = curses.color_pair(pairs.get_pair(curses.COLOR_RED)) | curses.A_BOLD
    else:
        text = (
            f" Score: {sim.score}   Theme: {theme.name}   "
            f"[Arrows] Move   [/] Menu   [q] Quit"
        )
        attr = _theme_attr(pairs, theme.hud_color, theme.hud_attr)
    try:
        stdscr.addstr(row, 0, " " * max(0, max_x - 1))
        stdscr.addstr(row, 0, text[: max(0, max_x - 1)], attr)
    except curses.error:
        pass


def _draw_game_over_banner(stdscr, sim, max_x):
    msg = f" GAME OVER   Score: {sim.score}   [r] Restart   [q] Quit "
    row = 1 + sim.height // 2
    col = max(0, (max_x - len(msg)) // 2)
    attr = curses.A_BOLD | curses.A_REVERSE
    try:
        stdscr.addstr(row, col, msg[: max(0, max_x - col - 1)], attr)
    except curses.error:
        pass


def render(stdscr, sim, pairs, max_x, theme, player_glyph, obstacle_glyph):
    """격자 테두리 + 텔레그래프 + 장애물 + 플레이어 + HUD를 한 프레임 그린다.

    theme은 색·속성만 결정한다. player_glyph/obstacle_glyph는 스킨(테마와
    분리된 별도 선택)이 결정하는 글리프 문자 — 메뉴 미리보기는 호출측이
    THEMES[menu_state.theme_idx] / menu_state.player_char / .enemy_char를
    넘기는 것으로 구현된다(sim은 그대로 두고 화면만 바뀐다). 텔레그래프는
    스킨 대상이 아니라 항상 TELEGRAPH_GLYPH 고정.
    """
    stdscr.erase()
    ox, oy = 1, 1  # 왼쪽/위 테두리 두께만큼의 오프셋

    border_attr = _theme_attr(pairs, theme.border_color, theme.border_attr)
    try:
        border_row = "+" + "-" * sim.width + "+"
        stdscr.addstr(0, 0, border_row[: max(0, max_x - 1)], border_attr)
        stdscr.addstr(oy + sim.height, 0, border_row[: max(0, max_x - 1)], border_attr)
        for row in range(sim.height):
            stdscr.addstr(oy + row, 0, "|", border_attr)
            if ox + sim.width < max_x:
                stdscr.addstr(oy + row, ox + sim.width, "|", border_attr)
    except curses.error:
        pass

    # 텔레그래프를 먼저 그리고 그 위에 실제 장애물을 덧그린다(같은 칸이면
    # 실제 장애물이 우선해 보이게).
    telegraph_attr = _theme_attr(pairs, theme.telegraph_color, theme.telegraph_attr)
    for obs in sim.obstacles:
        nx, ny = obs.next_pos()
        if sim.in_bounds(nx, ny):
            try:
                stdscr.addstr(oy + ny, ox + nx, TELEGRAPH_GLYPH, telegraph_attr)
            except curses.error:
                pass

    obstacle_attr = _theme_attr(pairs, theme.obstacle_color, theme.obstacle_attr)
    for obs in sim.obstacles:
        try:
            stdscr.addstr(oy + obs.y, ox + obs.x, obstacle_glyph, obstacle_attr)
        except curses.error:
            pass

    player_attr = _theme_attr(pairs, theme.player_color, theme.player_attr)
    try:
        stdscr.addstr(oy + sim.player_y, ox + sim.player_x, player_glyph, player_attr)
    except curses.error:
        pass

    hud_row = oy + sim.height + 1
    draw_hud(stdscr, hud_row, max_x, sim, pairs, theme)

    if sim.game_over:
        _draw_game_over_banner(stdscr, sim, max_x)

    stdscr.refresh()


def run_game(stdscr):
    """curses 진입점: 초기화, 턴제 입력 루프, 종료 시 자동 복원(curses.wrapper)."""
    try:
        curses.curs_set(0)
    except curses.error:
        # curs_set을 지원하지 않는 terminfo(TERM=vt100/dumb 등)에서는 ERR을 던진다.
        # 커서를 숨기지 못할 뿐 게임 자체는 정상 동작하므로 무시한다.
        pass
    stdscr.nodelay(False)  # 턴제 게임이라 키 입력을 블로킹으로 기다린다
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass

    _wait_for_bigger_terminal(stdscr)

    rng = random.Random()
    pairs = ColorPairCache()
    max_y, max_x = stdscr.getmaxyx()
    sim = _make_sim_for_screen(max_y, max_x, rng)

    # 테마/스킨은 프로세스 로컬 상태일 뿐 저장하지 않는다 — 항상 THEMES[0]
    # (Classic) + 기본 글리프로 시작하고, 종료하면 다음 실행은 다시 기본값이다.
    theme_index = 0
    player_glyph = DEFAULT_PLAYER_GLYPH
    obstacle_glyph = DEFAULT_ENEMY_GLYPH
    menu_open = False
    menu_state = None

    quit_game = False
    while not quit_game:
        max_y, max_x = stdscr.getmaxyx()
        if _too_small(max_y, max_x):
            _wait_for_bigger_terminal(stdscr)
            max_y, max_x = stdscr.getmaxyx()
            sim = _make_sim_for_screen(max_y, max_x, rng)

        # 메뉴가 열려있으면 배경을 커서가 가리키는 테마/나/적 스킨으로 그려
        # 실시간 미리보기한다(실제 확정 전까지 theme_index/player_glyph/
        # obstacle_glyph/sim은 그대로).
        if menu_open:
            active_theme = THEMES[menu_state.theme_idx]
            active_player_glyph = menu_state.player_char
            active_obstacle_glyph = menu_state.enemy_char
        else:
            active_theme = THEMES[theme_index]
            active_player_glyph = player_glyph
            active_obstacle_glyph = obstacle_glyph
        render(stdscr, sim, pairs, max_x, active_theme, active_player_glyph, active_obstacle_glyph)
        if menu_open:
            draw_skin_menu(stdscr, menu_state, THEMES, max_y, max_x)
            stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_RESIZE:
            # 다음 루프에서 새 크기로 다시 계산한다. 진행 중이던 판은 안전하게
            # 새로 시작한다(리사이즈 중간 상태를 억지로 이어붙이지 않음).
            max_y, max_x = stdscr.getmaxyx()
            if _too_small(max_y, max_x):
                _wait_for_bigger_terminal(stdscr)
                max_y, max_x = stdscr.getmaxyx()
            sim = _make_sim_for_screen(max_y, max_x, rng)
            continue

        if menu_open:
            # 메뉴가 열려있는 동안은 게임 턴이 절대 진행되지 않는다 — 아래
            # 키 처리는 메뉴 칸이동/커서/확정/취소만 다루고 그대로 다음
            # 프레임으로.
            menu_state, cmd = resolve_menu_key(key, menu_state, len(THEMES))
            if cmd == "confirm":
                theme_index = menu_state.theme_idx
                player_glyph = menu_state.player_char
                obstacle_glyph = menu_state.enemy_char
                menu_open = False
            elif cmd == "cancel":
                menu_open = False
            continue

        if key == MENU_KEY:
            # 게임오버 화면에서도 동작한다 — sim 상태는 건드리지 않는다.
            menu_open = True
            menu_state = make_menu_state(theme_index, player_glyph, obstacle_glyph)
            continue

        action, command = resolve_key(key)
        if command == "quit":
            quit_game = True
        elif command == "restart":
            if sim.game_over:
                sim.reset()
        elif action is not None and not sim.game_over:
            sim.step(action)


# ============================================================
# Headless self-test (curses 없이 순수 로직 검증)
# ============================================================


def _snapshot(sim):
    """결정성 비교용 상태 스냅샷(장애물 순서 무관하게 정렬해서 비교)."""
    obs = tuple(sorted((o.x, o.y, o.dx, o.dy) for o in sim.obstacles))
    return (sim.player_x, sim.player_y, sim.turn, sim.score, sim.game_over, obs)


def run_selftest():
    """--selftest: curses 없이 DodgeSim 불변식을 검증한다."""
    results = []
    ok = True

    # 1) 장애물이 플레이어 칸에 오면 game_over가 True
    #    (wait이 없으므로 "right" 이동 후 플레이어의 새 칸으로 장애물이
    #    들어오도록 장애물 위치를 잡는다 — 대기 없이도 직접 충돌을 재현)
    try:
        sim = DodgeSim(10, 10, rng=random.Random(1))
        assert (sim.player_x, sim.player_y) == (5, 5)
        sim.obstacles = [Obstacle(6, 4, 0, 1)]  # 다음 턴 (6,5) = 플레이어가 right로 이동할 칸
        sim.step("right")
        assert (sim.player_x, sim.player_y) == (6, 5), "right 이동 결과 좌표가 예상과 다름"
        assert sim.game_over is True, "장애물이 플레이어 칸에 들어왔는데 game_over가 아님"
        results.append("PASS: 직접 충돌 -> game_over")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 직접 충돌 -> game_over - {e}")

    # 2) 정상 진행 시 턴 카운터가 정확히 1씩 증가 + 점수와 일치
    #    (제자리에 거의 머무르도록 up/down을 번갈아 5번 — 대기 대체)
    try:
        sim = DodgeSim(30, 30, rng=random.Random(2))
        moves = ["up", "down", "up", "down", "up"]
        for i, mv in enumerate(moves):
            before = sim.turn
            sim.step(mv)
            assert sim.turn == before + 1, f"턴이 1씩 증가하지 않음(turn={sim.turn}, before={before})"
            assert sim.score == sim.turn, "score가 turn과 불일치"
        assert not sim.game_over, "이 테스트 시나리오에서 예상치 못하게 게임오버됨"
        results.append("PASS: 턴 카운터 +1 및 score==turn")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 턴 카운터 +1 및 score==turn - {e}")

    # 3) 벽 경계 - 가장자리에서 바깥 방향 입력 시 좌표 불변(턴은 소비됨)
    try:
        sim = DodgeSim(10, 10, rng=random.Random(3))
        sim.player_x, sim.player_y = 0, 0
        sim.obstacles = []
        before_turn = sim.turn
        sim.step("left")
        assert (sim.player_x, sim.player_y) == (0, 0), "좌상단에서 left인데 좌표가 바뀜"
        assert sim.turn == before_turn + 1, "벽에 막혀도 턴은 소비돼야 함(left)"
        sim.obstacles = []
        before_turn = sim.turn
        sim.step("up")
        assert (sim.player_x, sim.player_y) == (0, 0), "좌상단에서 up인데 좌표가 바뀜"
        assert sim.turn == before_turn + 1, "벽에 막혀도 턴은 소비돼야 함(up)"

        sim2 = DodgeSim(10, 10, rng=random.Random(3))
        sim2.player_x, sim2.player_y = sim2.width - 1, sim2.height - 1
        sim2.obstacles = []
        before_turn = sim2.turn
        sim2.step("right")
        assert (sim2.player_x, sim2.player_y) == (9, 9), "우하단에서 right인데 좌표가 바뀜"
        assert sim2.turn == before_turn + 1, "벽에 막혀도 턴은 소비돼야 함(right)"
        sim2.obstacles = []
        before_turn = sim2.turn
        sim2.step("down")
        assert (sim2.player_x, sim2.player_y) == (9, 9), "우하단에서 down인데 좌표가 바뀜"
        assert sim2.turn == before_turn + 1, "벽에 막혀도 턴은 소비돼야 함(down)"
        results.append("PASS: 벽 경계 - 좌표 불변 + 턴 소비")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 벽 경계 - {e}")

    # 4) pass-through(스쳐 지나가기) 충돌 - 플레이어와 장애물이 서로 맞바꿔 통과
    try:
        sim = DodgeSim(10, 10, rng=random.Random(4))
        assert (sim.player_x, sim.player_y) == (5, 5)
        sim.obstacles = [Obstacle(4, 5, 1, 0)]  # 플레이어 (5,5)->(4,5), 장애물 (4,5)->(5,5)
        sim.step("left")
        assert sim.game_over is True, "맞바꿔 통과했는데 game_over가 아님(pass-through 미탐지)"
        results.append("PASS: pass-through 충돌 탐지")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: pass-through 충돌 탐지 - {e}")

    # 5) 격자 밖으로 나간 장애물이 리스트에서 제거됨
    #    (장애물과 무관한 방향으로 이동 — "wait" 대신 "up")
    try:
        sim = DodgeSim(10, 10, rng=random.Random(5))
        leaving = Obstacle(0, 5, -1, 0)  # 다음 턴 x=-1로 격자 이탈
        sim.obstacles = [leaving]
        sim.step("up")
        assert leaving not in sim.obstacles, "격자 밖으로 나간 장애물이 제거되지 않음"
        results.append("PASS: 격자 이탈 장애물 제거")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 격자 이탈 장애물 제거 - {e}")

    # 6) 시드 고정 시 동일 입력열 -> 동일 최종 상태(결정성)
    try:
        actions = ["up", "down", "left", "right"] * 30
        sim_a = DodgeSim(15, 15, rng=random.Random(777))
        sim_b = DodgeSim(15, 15, rng=random.Random(777))
        for a in actions:
            sim_a.step(a)
            sim_b.step(a)
        assert _snapshot(sim_a) == _snapshot(sim_b), "같은 시드+입력열인데 최종 상태가 다름"
        results.append("PASS: 시드 고정 결정성")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 시드 고정 결정성 - {e}")

    # 7) 랜덤 입력으로 2000턴(게임오버 시 재시작) 무예외 완주 + 좌표가 항상 격자 안
    try:
        sim = DodgeSim(12, 12, rng=random.Random(2024))
        action_rng = random.Random(99)
        action_names = list(ACTIONS.keys())
        restarts = 0
        for _ in range(2000):
            sim.step(action_rng.choice(action_names))
            assert 0 <= sim.player_x < sim.width and 0 <= sim.player_y < sim.height, "플레이어가 격자 밖으로 나감"
            for o in sim.obstacles:
                assert 0 <= o.x < sim.width and 0 <= o.y < sim.height, "장애물이 격자 밖에 남아있음"
            if sim.game_over:
                sim.reset()
                restarts += 1
        results.append(f"PASS: 2000턴 무예외 완주(재시작 {restarts}회)")
    except Exception as e:  # noqa: BLE001 - self-test는 모든 예외를 포착해 보고해야 함
        ok = False
        results.append(f"FAIL: 2000턴 무예외 완주 - {type(e).__name__}: {e}")

    # 8) 게임오버 후 재시작 시 상태가 초기화됨(턴 0, 장애물 비움)
    try:
        sim = DodgeSim(10, 10, rng=random.Random(8))
        sim.obstacles = [Obstacle(6, 4, 0, 1)]  # 다음 턴 (6,5) = 플레이어가 right로 이동할 칸
        sim.step("right")
        assert sim.game_over is True
        sim.reset()
        assert sim.turn == 0, "재시작 후 turn이 0이 아님"
        assert sim.obstacles == [], "재시작 후 장애물이 비어있지 않음"
        assert sim.game_over is False, "재시작 후에도 game_over가 True"
        assert (sim.player_x, sim.player_y) == (5, 5), "재시작 후 플레이어가 중앙이 아님"
        results.append("PASS: 재시작 시 상태 초기화")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 재시작 시 상태 초기화 - {e}")

    # 9) step()은 4방향 action만 받는다 — 그 외(과거의 "wait" 포함)는 KeyError를
    #    던지고 상태를 전혀 바꾸지 않는다(턴 미진행). 이것이 이번에 고정한 계약이다.
    try:
        sim = DodgeSim(10, 10, rng=random.Random(42))
        for bad in ("wait", "h", "j", "k", "l", "", None, "diagonal"):
            before = _snapshot(sim)
            raised = False
            try:
                sim.step(bad)
            except KeyError:
                raised = True
            assert raised, f"step()이 유효하지 않은 action({bad!r})에 KeyError를 던지지 않음"
            assert _snapshot(sim) == before, f"유효하지 않은 action({bad!r}) 이후 상태가 변경됨(턴 미진행이어야 함)"
        results.append("PASS: step()은 4방향 action만 허용, 그 외엔 KeyError+상태 불변")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: step() 4방향 전용 계약 - {e}")

    # 10) 테마 카탈로그가 정확히 10개이고, 각 테마가 색 필드를 빠짐없이 갖는다
    #     (글리프는 더 이상 테마 필드가 아니다 — Theme는 색·속성 전용).
    try:
        assert len(THEMES) == 10, f"테마가 10개가 아님(len={len(THEMES)})"
        names = [t.name for t in THEMES]
        assert len(set(names)) == 10, f"테마 이름이 중복됨: {names}"
        for t in THEMES:
            assert not hasattr(t, "glyph_player"), f"{t.name}에 glyph_player가 여전히 남아있음(테마 분리 실패)"
            for role in ("player", "obstacle", "telegraph", "border", "hud"):
                assert hasattr(t, f"{role}_color"), f"{t.name}에 {role}_color 없음"
                assert hasattr(t, f"{role}_attr"), f"{t.name}에 {role}_attr 없음"
        results.append("PASS: 테마 카탈로그 10종 + 색 필드 완비(글리프 필드 없음)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 테마 카탈로그 - {e}")

    # 10a) 기존 5개 테마(Classic/Mono/Neon/Amber/Ocean)의 색 구성은 이번
    #      확장(작업 A)으로 절대 안 바뀐다 — ext/fallback8이 같은 표준 8색
    #      값이어야 한다(Mono는 색 자체가 None이라 그대로 None인지만 확인).
    try:
        _ORIGINAL_5 = {
            "Classic": (curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_WHITE, curses.COLOR_WHITE),
            "Neon": (curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_MAGENTA),
            "Amber": (curses.COLOR_YELLOW, curses.COLOR_YELLOW, curses.COLOR_YELLOW, curses.COLOR_YELLOW, curses.COLOR_YELLOW),
            "Ocean": (curses.COLOR_WHITE, curses.COLOR_BLUE, curses.COLOR_CYAN, curses.COLOR_CYAN, curses.COLOR_WHITE),
        }
        by_name = {t.name: t for t in THEMES}
        for name, (p, o, tg, b, h) in _ORIGINAL_5.items():
            t = by_name[name]
            for field, expected in (
                ("player_color", p), ("obstacle_color", o),
                ("telegraph_color", tg), ("border_color", b), ("hud_color", h),
            ):
                color = getattr(t, field)
                assert color is not None and color.ext == expected and color.fallback8 == expected, (
                    f"{name}.{field}가 원래 색({expected})에서 바뀜: {color!r}"
                )
        mono = by_name["Mono"]
        for field in ("player_color", "obstacle_color", "telegraph_color", "border_color", "hud_color"):
            assert getattr(mono, field) is None, f"Mono.{field}가 더 이상 None이 아님(무채색 계약 위반)"
        results.append("PASS: 기존 5개 테마 색 구성 불변(작업 A 제약)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 기존 테마 색 구성 불변 - {e}")

    # 10b) 신규 5개 테마(Forest/Sunset/Ice/Toxic/Crimson) 각각의 플레이어·적
    #      색 조합(확장색 기준)이 다른 어떤 테마와도 겹치지 않는다(작업 B
    #      핵심 요구). None(Mono)도 포함해 10개 전부가 서로 다른 (player_ext,
    #      obstacle_ext) 쌍이어야 한다 — 단순 튜플 전체 비교가 아니라 딱 이
    #      두 필드만 뽑아 비교한다(화면에서 가장 눈에 띄는 두 요소이므로).
    try:
        pairs = []
        for t in THEMES:
            p_ext = t.player_color.ext if t.player_color is not None else None
            o_ext = t.obstacle_color.ext if t.obstacle_color is not None else None
            pairs.append((t.name, p_ext, o_ext))
        seen = {}
        for name, p_ext, o_ext in pairs:
            key = (p_ext, o_ext)
            assert key not in seen, f"{name}의 (플레이어,적) 색 조합 {key}가 {seen.get(key)}와 중복됨"
            seen[key] = name
        assert len(seen) == len(THEMES), "플레이어/적 색 조합 유일성 집계가 테마 수와 불일치"
        results.append("PASS: 테마 10종 전부 (플레이어,적) 확장색 조합이 서로 유일함")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 테마 색 조합 유일성 - {e}")

    # 10c) 테마 이름 전부 유일 + 메뉴 칸 폭에 들어가는 길이 제한(10자 이내).
    try:
        names = [t.name for t in THEMES]
        assert len(set(names)) == len(names), f"테마 이름 중복: {names}"
        for n in names:
            assert len(n) <= 10, f"테마 이름 {n!r}가 10자를 넘음(len={len(n)}) - 메뉴 칸에 안 들어갈 수 있음"
        results.append("PASS: 테마 이름 전부 유일 + 10자 이내")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 테마 이름 유일성/길이 - {e}")

    # 10d) 모든 테마의 8색 폴백 값이 유효한 curses.COLOR_*(0~7) 범위 안이다
    #      (256색 미지원 터미널에서 크래시 없이 동작하려면 필수).
    try:
        for t in THEMES:
            for role in ("player", "obstacle", "telegraph", "border", "hud"):
                color = getattr(t, f"{role}_color")
                if color is None:
                    continue  # Mono - 색 자체가 없음(방어 대상 아님)
                assert 0 <= color.fallback8 <= 7, (
                    f"{t.name}.{role}_color.fallback8({color.fallback8})가 0~7 범위 밖"
                )
        results.append("PASS: 모든 테마의 8색 폴백 값이 curses.COLOR_* 범위(0~7) 안")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 8색 폴백 범위 - {e}")

    # 11) 메뉴 커서 이동이 경계(0/마지막)에서 안전하게 순환한다(순수 함수,
    #     테마 색인·스킨 색인·칸 이동 셋 다 이 헬퍼를 공유한다)
    try:
        assert _move_menu_selection(0, -1, 5) == 4, "0에서 위로 가면 마지막으로 순환해야 함"
        assert _move_menu_selection(4, 1, 5) == 0, "마지막에서 아래로 가면 처음으로 순환해야 함"
        assert _move_menu_selection(2, 1, 5) == 3, "중간에서 아래로 1 이동이 어긋남"
        assert _move_menu_selection(2, -1, 5) == 1, "중간에서 위로 1 이동이 어긋남"
        assert _move_menu_selection(0, -1, 0) == 0, "count=0(빈 목록) 방어가 동작하지 않음"
        results.append("PASS: 메뉴 커서 순환/클램프 경계 안전")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 메뉴 커서 순환/클램프 - {e}")

    # 12) 나/적 스킨 후보 목록 - 전부 단일폭 ASCII이고 기본값이 후보[0]과 일치
    try:
        for label, candidates, default in (
            ("PLAYER_SKINS", PLAYER_SKINS, DEFAULT_PLAYER_GLYPH),
            ("ENEMY_SKINS", ENEMY_SKINS, DEFAULT_ENEMY_GLYPH),
        ):
            assert 6 <= len(candidates) <= 8, f"{label} 후보 개수가 6~8개 범위 밖: {len(candidates)}"
            for ch in candidates:
                assert isinstance(ch, str) and len(ch) == 1 and ch.isascii(), (
                    f"{label}의 {ch!r}가 단일폭 ASCII 문자가 아님"
                )
            assert candidates[0] == default, f"{label}[0]({candidates[0]!r})이 기본값({default!r})과 다름"
        assert len(TELEGRAPH_GLYPH) == 1, "TELEGRAPH_GLYPH가 단일 문자가 아님"
        results.append("PASS: 나/적 스킨 후보 목록 - 단일폭 ASCII + 기본값 일치")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 스킨 후보 목록 - {e}")

    # 13) 3칸 메뉴 - 좌우(←→) 키가 칸을 3칸 순환 이동시킨다(테마<->적<->나)
    try:
        state = make_menu_state(0, DEFAULT_PLAYER_GLYPH, DEFAULT_ENEMY_GLYPH)
        assert state.active_col == MENU_COL_THEME, "메뉴를 처음 열면 테마 칸이 활성이어야 함"
        state, cmd = resolve_menu_key(curses.KEY_RIGHT, state, 5)
        assert state.active_col == MENU_COL_PLAYER and cmd is None, "테마->나 칸 이동이 어긋남"
        state, cmd = resolve_menu_key(curses.KEY_RIGHT, state, 5)
        assert state.active_col == MENU_COL_ENEMY and cmd is None, "나->적 칸 이동이 어긋남"
        state, cmd = resolve_menu_key(curses.KEY_RIGHT, state, 5)
        assert state.active_col == MENU_COL_THEME and cmd is None, "나 칸에서 오른쪽 - 테마로 순환해야 함"
        state, cmd = resolve_menu_key(curses.KEY_LEFT, state, 5)
        assert state.active_col == MENU_COL_ENEMY and cmd is None, "테마 칸에서 왼쪽 - 적 칸으로 순환해야 함"
        results.append("PASS: 3칸 메뉴 좌우 칸 이동(←→ 3칸 순환)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 3칸 메뉴 칸 이동 - {e}")

    # 14) 3칸 메뉴 - 위아래(↑↓)가 활성 칸 안에서 커서를 이동시킨다. 테마 칸은
    #     테마 색인, 나/적 칸은 스킨 후보 색인(직접입력=None 상태에서도 안전 진입).
    try:
        state = make_menu_state(0, DEFAULT_PLAYER_GLYPH, DEFAULT_ENEMY_GLYPH)
        state, _ = resolve_menu_key(curses.KEY_UP, state, 5)
        assert state.theme_idx == 4, "테마 칸에서 위 - 마지막 테마로 순환해야 함"

        state, _ = resolve_menu_key(curses.KEY_RIGHT, state, 5)  # -> 나 칸
        assert state.active_col == MENU_COL_PLAYER
        state, _ = resolve_menu_key(curses.KEY_DOWN, state, 5)
        assert state.player_idx == 1 and state.player_char == PLAYER_SKINS[1], "나 칸 아래 이동이 어긋남"

        # 직접 입력(None) 상태에서 위/아래 진입 - 항상 유효한 색인에 안착해야 함
        custom_state = state._replace(player_idx=None, player_char="Q")
        down_state, _ = resolve_menu_key(curses.KEY_DOWN, custom_state, 5)
        assert down_state.player_idx == 0, "직접입력 상태에서 아래 - 첫 후보로 진입해야 함"
        up_state, _ = resolve_menu_key(curses.KEY_UP, custom_state, 5)
        assert up_state.player_idx == len(PLAYER_SKINS) - 1, "직접입력 상태에서 위 - 마지막 후보로 진입해야 함"
        results.append("PASS: 3칸 메뉴 칸 안 커서 이동(↑↓, 직접입력 경계 포함)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 3칸 메뉴 칸 안 커서 이동 - {e}")

    # 15) 나/적 칸에서 인쇄 가능 ASCII 문자 키는 즉시 그 칸의 스킨이 된다("직접
    #     입력"). 테마 칸에서는 무시되고(숫자 즉시선택 제거), 방향키·제어문자는
    #     어느 칸에서도 문자로 해석되지 않는다.
    try:
        assert _printable_ascii_char(ord("O")) == "O", "'O'가 인쇄 가능 ASCII로 해석되지 않음"
        assert _printable_ascii_char(ord("!")) == "!", "'!'(33, 범위 하한)가 거부됨"
        assert _printable_ascii_char(ord("~")) == "~", "'~'(126, 범위 상한)이 거부됨"
        assert _printable_ascii_char(ord(" ")) is None, "스페이스(32)가 허용됨(제외 대상)"
        assert _printable_ascii_char(27) is None, "ESC(27) 제어문자가 문자로 해석됨"
        assert _printable_ascii_char(10) is None, "개행(10) 제어문자가 문자로 해석됨"
        assert _printable_ascii_char(curses.KEY_UP) is None, "방향키(curses 특수키)가 문자로 해석됨"
        assert _printable_ascii_char(curses.KEY_RIGHT) is None, "방향키(curses 특수키)가 문자로 해석됨"

        state = make_menu_state(0, DEFAULT_PLAYER_GLYPH, DEFAULT_ENEMY_GLYPH)
        state, _ = resolve_menu_key(curses.KEY_RIGHT, state, 5)  # -> 나 칸
        state, cmd = resolve_menu_key(ord("O"), state, 5)
        assert state.player_char == "O" and state.player_idx == PLAYER_SKINS.index("O") and cmd is None, (
            "나 칸에서 'O' 직접입력(후보 안)이 어긋남"
        )
        state, cmd = resolve_menu_key(ord("Q"), state, 5)  # 후보에 없는 문자
        assert state.player_char == "Q" and state.player_idx is None and cmd is None, (
            "나 칸에서 'Q' 직접입력(후보 밖 -> idx=None)이 어긋남"
        )

        state, _ = resolve_menu_key(curses.KEY_RIGHT, state, 5)  # -> 적 칸
        state, cmd = resolve_menu_key(ord("#"), state, 5)
        assert state.enemy_char == "#" and cmd is None, "적 칸에서 '#' 직접입력이 어긋남"

        # 테마 칸(활성 칸 0)에서는 문자 키가 아무것도 안 바꾼다(숫자 즉시선택
        # 제거 - 나/적 칸의 자유 입력과 충돌하므로 방향키+Enter로 통일했다).
        theme_state = make_menu_state(0, DEFAULT_PLAYER_GLYPH, DEFAULT_ENEMY_GLYPH)
        unchanged, cmd = resolve_menu_key(ord("3"), theme_state, 5)
        assert unchanged == theme_state and cmd is None, "테마 칸에서 숫자 키가 여전히 즉시선택을 함(제거 대상)"

        results.append("PASS: 나/적 칸 자유 문자 입력(인쇄가능 ASCII만, 테마 칸 무시, 제어문자/방향키 거부)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 자유 문자 입력 해석 - {e}")

    # 16) 3칸 메뉴 - Enter 확정 / ESC·`/`·q 취소는 활성 칸과 무관하게 항상 동작
    try:
        state = make_menu_state(0, DEFAULT_PLAYER_GLYPH, DEFAULT_ENEMY_GLYPH)
        state, _ = resolve_menu_key(curses.KEY_RIGHT, state, 5)  # -> 나 칸
        state, _ = resolve_menu_key(ord("O"), state, 5)
        for enter_key in (10, 13, curses.KEY_ENTER):
            confirmed, cmd = resolve_menu_key(enter_key, state, 5)
            assert cmd == "confirm" and confirmed.player_char == "O", f"Enter({enter_key!r}) 확정이 어긋남"
        for cancel_key in (27, ord("/"), ord("q")):
            cancelled, cmd = resolve_menu_key(cancel_key, state, 5)
            assert cmd == "cancel" and cancelled == state, f"취소 키 {cancel_key!r}가 어긋남"
        results.append("PASS: 3칸 메뉴 확정/취소(Enter/ESC·`/`·q, 칸 무관)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 3칸 메뉴 확정/취소 - {e}")

    # 17) compute_scroll_window - 목록이 창보다 짧으면 스크롤이 아예
    #     일어나지 않는다(항상 (0, total)을 반환).
    try:
        assert compute_scroll_window(5, 10, 2) == (0, 5), "목록<창인데 스크롤이 일어남"
        assert compute_scroll_window(5, 5, 0) == (0, 5), "목록==창(경계)인데 스크롤이 일어남"
        assert compute_scroll_window(10, 0, 3) == (0, 0), "visible_rows<=0 방어가 동작하지 않음"
        results.append("PASS: compute_scroll_window - 목록이 창보다 짧으면 스크롤 없음")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: compute_scroll_window(스크롤 불필요) - {e}")

    # 18) compute_scroll_window - 커서가 맨 위/맨 아래/가운데일 때 각각 반환된
    #     창 [start, end) 안에 커서가 항상 포함되고, 위/아래 끝에서는 창이
    #     목록 경계를 넘지 않는다(작업 C 핵심 - 커서가 화면 밖으로 안 나감).
    try:
        total, visible = 10, 4
        # 맨 위
        start, end = compute_scroll_window(total, visible, 0)
        assert start == 0 and end == visible, f"커서 맨 위인데 창이 (0,{visible})이 아님: {(start, end)}"
        assert start <= 0 < end, "맨 위 커서가 창 안에 없음"
        # 맨 아래
        start, end = compute_scroll_window(total, visible, total - 1)
        assert end == total, f"커서 맨 아래인데 창 끝이 목록 끝이 아님: {(start, end)}"
        assert start <= total - 1 < end, "맨 아래 커서가 창 안에 없음"
        # 가운데
        cursor = 5
        start, end = compute_scroll_window(total, visible, cursor)
        assert start <= cursor < end, f"가운데 커서({cursor})가 창 {(start, end)} 안에 없음"
        assert 0 <= start and end <= total, f"창 {(start, end)}가 목록 경계를 벗어남"
        assert end - start == visible, f"창 폭이 visible_rows({visible})와 다름: {end - start}"
        results.append("PASS: compute_scroll_window - 커서가 위/아래/가운데여도 항상 창 안에 보임")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: compute_scroll_window(커서 가시성) - {e}")

    # 19) _active_row_index - 활성 칸에 따라 올바른 행 색인을 돌려준다(테마
    #     칸은 테마 색인 그대로, 나/적 칸은 스킨 색인이거나 직접입력(None)이면
    #     후보 목록 다음 줄(Custom 줄) 색인).
    try:
        state = make_menu_state(3, DEFAULT_PLAYER_GLYPH, DEFAULT_ENEMY_GLYPH)
        assert _active_row_index(state) == 3, "테마 칸 활성일 때 theme_idx를 그대로 안 돌려줌"

        state, _ = resolve_menu_key(curses.KEY_RIGHT, state, len(THEMES))  # -> 나 칸
        state, _ = resolve_menu_key(curses.KEY_DOWN, state, len(THEMES))
        assert _active_row_index(state) == state.player_idx, "나 칸 활성일 때 player_idx를 안 돌려줌"

        custom_state = state._replace(player_idx=None, player_char="Q")
        assert _active_row_index(custom_state) == len(PLAYER_SKINS), (
            "직접입력(None) 상태에서 Custom 줄 색인(len(candidates))을 안 돌려줌"
        )
        results.append("PASS: _active_row_index - 칸별 행 색인 계산")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: _active_row_index - {e}")

    # 난이도는 장기에도 증가하며 한 턴 경계에서 크게 뛰지 않는다.
    try:
        previous_chance, previous_count = spawn_parameters(0)
        for turn in range(1, 10001):
            chance, mean_count = spawn_parameters(turn)
            assert 0 < chance - previous_chance < 0.01, f"생성 확률 급변/정체: {turn}턴"
            assert 0 < mean_count - previous_count < 0.04, f"생성 개수 급변/정체: {turn}턴"
            previous_chance, previous_count = chance, mean_count
        chance, mean_count = spawn_parameters(10**9)
        assert 0 < chance < 1 and math.isfinite(mean_count), "장기 난이도 값이 유효하지 않음"
        assert mean_count > previous_count, "장기 생성 개수가 증가하지 않음"
        results.append("PASS: 난이도 연속 증가·턴 경계 완만함·장기 유효성")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 난이도 곡선 - {e}")

    # 실제 step 경로에서 정수 경계 양쪽의 추가 1개 확률과 미생성을 확인한다.
    try:
        class WaveRng:
            def __init__(self, *rolls):
                self.rolls = iter(rolls)

            def random(self):
                return next(self.rolls)

        for turn, low, high in ((39, 1, 2), (40, 2, 2), (41, 2, 3), (1000, 5, 6)):
            for roll, expected in ((0.0, high), (0.999999, low)):
                sim = DodgeSim(20, 20, rng=WaveRng(0.0, roll))
                sim.turn = turn - 1
                sim._make_spawn_candidate = lambda: Obstacle(0, 0, 1, 0)
                sim.step("right")
                assert len(sim.obstacles) == expected, f"{turn}턴 확률 반올림 실패"
                assert sim.turn == turn and not sim.game_over, "턴 진행 실패"
        sim = DodgeSim(20, 20, rng=WaveRng(0.999999))
        sim.turn = 999
        sim.step("right")
        assert not sim.obstacles, "생성하지 않는 확률이 사라짐"
        sim.reset()
        assert spawn_parameters(sim.turn) == spawn_parameters(0), "재시작 난이도 복원 실패"
        results.append("PASS: step의 확률 반올림·미생성·재시작 난이도 복원")
    except (AssertionError, StopIteration) as e:
        ok = False
        results.append(f"FAIL: 난이도 적용 - {e}")

    # 생성 위치 재시도까지 포함한 실제 평균량이 후반에도 늘어나는지 확인한다.
    try:
        averages = []
        for turn in (40, 100, 300, 1000):
            sim = DodgeSim(80, 24, rng=random.Random(20260917))
            sim.turn = turn
            total = 0
            for _ in range(3000):
                sim.obstacles = []
                sim._spawn_wave()
                total += len(sim.obstacles)
            averages.append(total / 3000)
        assert all(a < b for a, b in zip(averages, averages[1:])), f"후반 생성량 정체: {averages}"
        assert 0.8 < averages[0] < 1.2, f"40턴 생성량 이탈: {averages[0]}"
        assert 4.5 < averages[-1] < 5.3, f"1000턴 생성량 이탈: {averages[-1]}"
        results.append("PASS: 실제 생성량 40/100/300/1000턴 증가")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: 실제 생성량 - {e}")

    for line in results:
        print(line)

    if ok:
        print("SELFTEST PASS")
        return 0
    print("SELFTEST FAIL: 하나 이상의 검증 실패 (위 FAIL 항목 참고)")
    return 1


# ============================================================
# 진입점
# ============================================================

USAGE = """Usage: dodgegame.py [--selftest | --help]

A turn-based dodge game for the terminal (built with curses). One key
press is one turn -- dodge obstacles coming from every side and survive
as long as you can.

Options:
  (none)       Play the game
  --selftest   Run the game-logic self-test only (no curses screen)
  --help, -h   Show this help text

Controls:
  Arrow keys      Move one step (one press = one turn) -- you must pick
                  a direction every turn
  /               Open menu: Theme / You / Enemy (3 columns, no turn used)
  r               Restart after game over
  q               Quit

Menu (`/`) controls:
  Left/Right      Move between columns (Theme <-> You <-> Enemy, wraps)
  Up/Down         Move the cursor inside the active column
  Enter           Confirm
  ESC, /, q       Cancel (restores the settings from before you opened it)
  In the You or Enemy column, any printable key sets that column's glyph
  right away (even a character not in the list). Whatever the cursor
  points to is shown live on the game screen behind the menu.

Rules:
  The grid edge is a wall -- you cannot move past it (trying to still
  uses up your turn; you just stay in place). Obstacles spawn at the
  edges and move in a straight line (no homing); they vanish once they
  leave the grid. Each obstacle's next position is shown one turn ahead
  as a dim `·` (the telegraph). Game over when an obstacle enters your
  cell (including swapping places with you in one move). Score = turns
  survived. Spawn frequency and average wave size rise gradually as you
  survive, including beyond turn 40. There are 10 themes, and separate
  glyph choices for You and the Enemy in the `/` menu, but none of it is
  saved -- next time you run the game it starts back at the defaults. If
  the menu list is taller than your terminal, it scrolls to keep your
  cursor in view (look for `^`/`v` markers when there is more above or
  below).
"""


def _install_terminal_guards():
    """SIGTERM/SIGHUP을 SystemExit으로 바꿔 curses 정리가 반드시 돌게 한다.

    기본 처리(프로세스 즉시 종료)로 죽으면 `curses.wrapper`의 복원 코드가 실행되지
    않아, 게임을 실행했던 터미널이 noecho/cbreak·커서 숨김 상태로 남는다(실측:
    SIGHUP에서 ECHO/ICANON이 꺼진 채 유지됨). 남의 셸을 망가뜨리지 않도록 방어한다.
    """

    def _bail(signum, _frame):
        try:
            curses.endwin()
        except curses.error:
            pass
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(signum, _bail)
        except (ValueError, OSError):
            # 메인 스레드가 아니거나 플랫폼이 해당 시그널을 모르면 조용히 넘어간다.
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if "--selftest" in argv:
        return run_selftest()
    _install_terminal_guards()
    try:
        curses.wrapper(run_game)
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
