"""
Серверный движок игры «Краш» (ракета) для WXS Game.
Только стандартная библиотека — тестируется без aiohttp.

Что изменилось по сравнению с версией на Supabase:
  * crash_point и salt живут ТОЛЬКО в памяти сервера. Клиентам уходит SHA-256 хеш,
    а salt и итоговый коэффициент — после взрыва (provably fair сохраняется);
  * множитель при «Забрать» считает сервер по своим часам, клиент его не присылает;
  * раунды крутятся сами, независимо от того, открыто ли у кого-то приложение;
  * все ставки текущего раунда пишутся в sqlite-журнал: если сервер упал посреди
    раунда, при старте ставки возвращаются игрокам.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import sqlite3
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Optional

log = logging.getLogger("crash")

# ---- Константы: значения 1:1 с script.js ------------------------------------
CRASH_RTP = 0.95
WAIT_MS = 5000            # CRASH_WAIT_MS
HOLD_MS = 3000            # CRASH_RESULT_HOLD_MS
HISTORY_MAX = 25          # CRASH_HISTORY_MAX
GROWTH_PER_MS = math.log(2) / 10000
SLOW_START_MS = 6000
SLOW_START_TARGET = 1.5
MAX_CRASH = 1000.0
MIN_BET = 0.10
GAME_LABEL = "Краш"       # так игра называется в ленте live_bets


# ---- Утилиты ----------------------------------------------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def money(x: float) -> float:
    """Округление до центов (как roundMoney в script.js)."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---- Provably fair + математика полёта (порт calculateCrashPoint/computeCrashMultAt)
_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generate_salt(length: int = 32) -> str:
    return "".join(secrets.choice(_CHARSET) for _ in range(length))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def calculate_crash_point(seed: str, salt: str) -> float:
    int_val = int((seed + salt)[:8], 16)
    max_val = 4294967296  # 2^32
    raw = (CRASH_RTP * max_val) / (max_val - int_val)
    crash = math.floor(raw * 100) / 100
    crash = max(1.00, crash)
    return min(crash, MAX_CRASH)


def mult_at(elapsed_ms: float, cap: Optional[float] = None) -> float:
    e = max(0.0, elapsed_ms)
    if e < SLOW_START_MS:
        p = e / SLOW_START_MS
        raw = 1 + (SLOW_START_TARGET - 1) * p ** 3
    else:
        raw = SLOW_START_TARGET * math.exp(GROWTH_PER_MS * (e - SLOW_START_MS))
    floored = math.floor(raw * 100) / 100
    return min(cap, floored) if cap is not None else floored


def crash_time_ms(point: float) -> float:
    """Через сколько мс после старта полёта множитель дорастёт до point."""
    if point <= 1.0:
        return 0.0
    if point <= SLOW_START_TARGET:
        t = SLOW_START_MS * ((point - 1) / (SLOW_START_TARGET - 1)) ** (1 / 3)
    else:
        t = SLOW_START_MS + math.log(point / SLOW_START_TARGET) / GROWTH_PER_MS
    while mult_at(t) < point:  # страховка от погрешности float
        t += 1
    return t


# ---- Ошибки -----------------------------------------------------------------
class GameError(Exception):
    """Ошибка, текст которой можно показать игроку."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class InsufficientFunds(Exception):
    pass


class BalanceError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ---- Баланс (адаптер) -------------------------------------------------------
class Balance:
    """Интерфейс денег. Боевая реализация (Supabase) — в server.py."""

    async def profile(self, tg_id: int) -> Optional[dict]:
        """-> {"balance": float, "name": str, "avatar": str | None} или None."""
        raise NotImplementedError

    async def place_bet(self, tg_id: int, amount: float, game: str) -> float:
        """Атомарно списать ставку, вернуть новый баланс. InsufficientFunds, если не хватает."""
        raise NotImplementedError

    async def credit_win(self, tg_id: int, amount: float, mult: Optional[float]) -> float:
        """Начислить выигрыш, вернуть новый баланс."""
        raise NotImplementedError

    async def live_open(self, tg_id: int, name: str, amount: float, game: str) -> Any:
        return None

    async def live_close(self, live_id: Any, amount: float, win: float) -> None:
        return None


class DemoBalance(Balance):
    """Баланс в памяти — для локальных тестов (стартовые 100 $)."""

    def __init__(self, start: float = 100.0):
        self.start = start
        self.balances: dict[int, float] = {}
        self.live: list[dict] = []

    def _get(self, tg_id: int) -> float:
        return self.balances.setdefault(tg_id, self.start)

    async def profile(self, tg_id):
        return {"balance": self._get(tg_id), "name": f"Player {tg_id}", "avatar": None}

    async def place_bet(self, tg_id, amount, game):
        if self._get(tg_id) < amount:
            raise InsufficientFunds()
        self.balances[tg_id] = money(self.balances[tg_id] - amount)
        return self.balances[tg_id]

    async def credit_win(self, tg_id, amount, mult):
        self.balances[tg_id] = money(self._get(tg_id) + amount)
        return self.balances[tg_id]

    async def live_open(self, tg_id, name, amount, game):
        self.live.append({"tg": tg_id, "amount": amount, "win": None})
        return len(self.live) - 1

    async def live_close(self, live_id, amount, win):
        self.live[live_id]["win"] = win


# ---- Журнал (sqlite) --------------------------------------------------------
class Journal:
    """Хранит историю раундов и незавершённые деньги — переживает перезапуск."""

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.executescript(
            """
            pragma journal_mode=wal;
            create table if not exists history(
                id integer primary key autoincrement, crash_point real not null);
            create table if not exists open_bets(
                round_id text, tg_id integer, amount real, primary key(round_id, tg_id));
            create table if not exists pending_credits(
                id integer primary key autoincrement, tg_id integer, amount real, mult real);
            """
        )

    def history(self, limit: int) -> list[float]:
        rows = self.db.execute("select crash_point from history order by id desc limit ?", (limit,))
        return [r[0] for r in rows]

    def push_history(self, point: float) -> None:
        self.db.execute("insert into history(crash_point) values(?)", (point,))

    def next_game_number(self) -> int:
        return (self.db.execute("select coalesce(max(id), 0) from history").fetchone()[0]) + 1

    def add_bet(self, round_id: str, tg_id: int, amount: float) -> None:
        self.db.execute("insert or replace into open_bets values(?,?,?)", (round_id, tg_id, amount))

    def drop_bet(self, round_id: str, tg_id: int) -> None:
        self.db.execute("delete from open_bets where round_id=? and tg_id=?", (round_id, tg_id))

    def drop_round(self, round_id: str) -> None:
        self.db.execute("delete from open_bets where round_id=?", (round_id,))

    def open_bets(self) -> list[tuple]:
        return self.db.execute("select round_id, tg_id, amount from open_bets").fetchall()

    def add_pending(self, tg_id: int, amount: float, mult: float) -> int:
        cur = self.db.execute("insert into pending_credits(tg_id, amount, mult) values(?,?,?)",
                              (tg_id, amount, mult))
        return cur.lastrowid

    def del_pending(self, pid: int) -> None:
        self.db.execute("delete from pending_credits where id=?", (pid,))

    def pending(self) -> list[tuple]:
        return self.db.execute("select id, tg_id, amount, mult from pending_credits").fetchall()


# ---- Модель раунда ----------------------------------------------------------
@dataclass
class Player:
    id: int
    name: str
    avatar: Optional[str] = None


@dataclass
class Bet:
    id: int
    telegram_id: int
    name: str
    avatar: Optional[str]
    amount: float
    status: str = "pending"          # pending | active | cashed_out | lost
    cashout_mult: Optional[float] = None
    win_amount: Optional[float] = None
    live_id: Any = None

    def public(self) -> dict:
        return {
            "id": self.id, "telegram_id": self.telegram_id, "name": self.name,
            "avatar": self.avatar, "amount": self.amount, "status": self.status,
            "cashout_mult": self.cashout_mult, "win_amount": self.win_amount,
        }


class Round:
    def __init__(self, number: int, wait_ms: int):
        self.id = str(uuid.uuid4())
        self.number = number
        self.salt = generate_salt()
        self.hash = sha256_hex(self.salt)
        self.crash_point = calculate_crash_point(self.hash, self.salt)
        self.status = "waiting"      # waiting | flying | crashed
        self.created_at = now_ms()
        self.waiting_ends_at = self.created_at + wait_ms
        self.starts_at: Optional[int] = None
        self.crashed_at: Optional[int] = None
        self.bets: dict[int, Bet] = {}

    def public(self) -> dict:
        """То, что видят клиенты. salt и crash_point — только после взрыва."""
        done = self.status == "crashed"
        return {
            "id": self.id, "game_number": self.number, "status": self.status,
            "hash": self.hash,
            "salt": self.salt if done else "",
            "crash_point": self.crash_point if done else None,
            "created_at": iso(self.created_at),
            "waiting_ends_at": iso(self.waiting_ends_at),
            "starts_at": iso(self.starts_at),
            "crashed_at": iso(self.crashed_at),
        }


# ---- Движок -----------------------------------------------------------------
class CrashEngine:
    def __init__(self, balance: Balance, journal: Optional[Journal] = None, *,
                 wait_ms: int = WAIT_MS, hold_ms: int = HOLD_MS,
                 min_bet: float = MIN_BET, max_bet: float = 0.0):
        self.balance = balance
        self.journal = journal or Journal()
        self.wait_ms, self.hold_ms = wait_ms, hold_ms
        self.min_bet, self.max_bet = min_bet, max_bet
        self.history: deque[float] = deque(self.journal.history(HISTORY_MAX), maxlen=HISTORY_MAX)
        self.round: Optional[Round] = None
        self.on_change: Callable[[bool], None] = lambda immediate: None
        self._game_no = self.journal.next_game_number()
        self._bet_seq = 0
        self._tasks: set[asyncio.Task] = set()
        self._inflight: set[int] = set()

    # -- служебное --
    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _emit(self, immediate: bool = False) -> None:
        try:
            self.on_change(immediate)
        except Exception:
            log.exception("on_change failed")

    def snapshot(self) -> dict:
        r = self.round
        bets = [b.public() for b in r.bets.values() if b.status != "pending"] if r else []
        return {"type": "state", "server_time": now_ms(),
                "round": r.public() if r else None,
                "bets": bets, "history": list(self.history)}

    # -- главный цикл --
    async def run(self) -> None:
        await self._recover()
        self._spawn(self._retry_pending_loop())
        while True:
            try:
                await self._play_round()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка в раунде, перезапускаю цикл")
                await asyncio.sleep(1)

    async def _play_round(self) -> None:
        r = Round(self._game_no, self.wait_ms)
        self._game_no += 1
        self.round = r
        self._emit(True)
        await asyncio.sleep(max(0, r.waiting_ends_at - now_ms()) / 1000)

        r.status = "flying"
        r.starts_at = now_ms()
        self._emit(True)
        crash_at = r.starts_at + crash_time_ms(r.crash_point)
        await asyncio.sleep(max(0, crash_at - now_ms()) / 1000)

        # ВЗРЫВ. Весь блок синхронный (без await) — гонки с cash_out() невозможны.
        r.status = "crashed"
        r.crashed_at = now_ms()
        lost = [b for b in r.bets.values() if b.status == "active"]
        for b in lost:
            b.status, b.win_amount = "lost", 0.0
        self.journal.drop_round(r.id)   # проигравшим возвращать нечего
        self._emit(True)
        for b in lost:
            self._spawn(self._live_close(b))

        await asyncio.sleep(self.hold_ms / 1000)
        # В историю раунд попадает после паузы: клиент сам добавляет его при взрыве.
        self.history.appendleft(r.crash_point)
        self.journal.push_history(r.crash_point)

    # -- ставки --
    def _check_amount(self, amount: Any) -> float:
        try:
            value = float(amount)
        except (TypeError, ValueError):
            raise GameError("bad_amount", "Некорректная сумма ставки")
        if not math.isfinite(value):
            raise GameError("bad_amount", "Некорректная сумма ставки")
        value = money(value)
        if value < self.min_bet:
            raise GameError("min", f"Минимальная ставка — {self.min_bet:.2f} $!")
        if self.max_bet and value > self.max_bet:
            raise GameError("max", f"Максимальная ставка — {self.max_bet:.2f} $!")
        return value

    async def place_bet(self, player: Player, amount: Any) -> dict:
        r = self.round
        if r is None or r.status != "waiting" or now_ms() >= r.waiting_ends_at:
            raise GameError("closed", "Приём ставок закрыт — дождитесь следующего раунда")
        if player.id in r.bets:
            raise GameError("already", "Вы уже сделали ставку в этом раунде")
        amount = self._check_amount(amount)

        self._bet_seq += 1
        bet = Bet(self._bet_seq, player.id, player.name, player.avatar, amount)
        r.bets[player.id] = bet   # бронируем слот сразу: двойной тап не спишет дважды
        try:
            balance = await self.balance.place_bet(player.id, amount, GAME_LABEL)
        except InsufficientFunds:
            r.bets.pop(player.id, None)
            raise GameError("insufficient", "Недостаточно средств!")
        except Exception:
            log.exception("place_bet: списание не удалось")
            r.bets.pop(player.id, None)
            raise GameError("server", "Не удалось списать ставку. Проверьте соединение и попробуйте снова.")

        if r.status == "crashed":    # раунд успел закончиться, пока шло списание
            bet.status, bet.win_amount = "lost", 0.0
        else:
            bet.status = "active"
            self.journal.add_bet(r.id, player.id, amount)
        self._spawn(self._live_open(bet))
        self._emit()
        return {"balance": balance}

    async def cash_out(self, player: Player) -> dict:
        r = self.round
        if r is None or r.status != "flying":
            raise GameError("not_flying", "Забрать выигрыш можно только во время полёта")
        bet = r.bets.get(player.id)
        if bet is None or bet.status != "active":
            raise GameError("no_bet", "У вас нет активной ставки в этом раунде")

        mult = mult_at(now_ms() - r.starts_at)       # время — серверное
        if mult >= r.crash_point:
            raise GameError("crashed", "Ракета уже взорвалась")
        win = money(bet.amount * mult)

        # Всё до первого await — синхронно: статус меняется раньше, чем сработает взрыв.
        bet.status, bet.cashout_mult, bet.win_amount = "cashed_out", mult, win
        pid = self.journal.add_pending(player.id, win, mult)
        self.journal.drop_bet(r.id, player.id)
        self._emit()
        self._spawn(self._live_close(bet))

        balance = await self._credit(player.id, win, mult, pid)
        return {"mult": mult, "win": win, "balance": balance}

    # -- деньги --
    async def _credit(self, tg_id: int, win: float, mult: Optional[float], pid: int) -> Optional[float]:
        """Зачисляет выигрыш. Если Supabase недоступен — выигрыш остаётся в журнале
        и дозачисляется фоном (_retry_pending_loop), игрок его не теряет.
        NB: RPC resolve_win не идемпотентен — при таймауте «после успеха» возможен повтор."""
        self._inflight.add(pid)
        try:
            for attempt in range(3):
                try:
                    balance = await self.balance.credit_win(tg_id, win, mult)
                    self.journal.del_pending(pid)
                    return balance
                except Exception:
                    log.exception("credit_win: попытка %s не удалась (tg=%s, %s)", attempt + 1, tg_id, win)
                    await asyncio.sleep(0.4 * (attempt + 1))
            return None
        finally:
            self._inflight.discard(pid)

    async def _retry_pending_loop(self) -> None:
        while True:
            for pid, tg_id, amount, mult in self.journal.pending():
                if pid in self._inflight:
                    continue
                self._inflight.add(pid)
                try:
                    await self.balance.credit_win(tg_id, amount, mult)
                    self.journal.del_pending(pid)
                    log.warning("дозачислен отложенный выигрыш tg=%s %s", tg_id, amount)
                except Exception:
                    log.error("не удалось дозачислить tg=%s %s — повторю позже", tg_id, amount)
                finally:
                    self._inflight.discard(pid)
            await asyncio.sleep(15)

    async def _recover(self) -> None:
        """После падения сервера возвращаем ставки незавершённого раунда."""
        for round_id, tg_id, amount in self.journal.open_bets():
            try:
                await self.balance.credit_win(tg_id, amount, None)
                self.journal.drop_bet(round_id, tg_id)
                log.warning("возврат ставки после перезапуска: tg=%s %s", tg_id, amount)
            except Exception:
                log.exception("не удалось вернуть ставку tg=%s %s", tg_id, amount)

    # -- лента live_bets (бегущая строка в приложении) --
    async def _live_open(self, bet: Bet) -> None:
        try:
            bet.live_id = await self.balance.live_open(bet.telegram_id, bet.name, bet.amount, GAME_LABEL)
        except Exception:
            log.exception("live_open")
            return
        if bet.live_id is not None and bet.status in ("lost", "cashed_out"):
            await self._live_close(bet)

    async def _live_close(self, bet: Bet) -> None:
        if bet.live_id is None:      # ещё открывается — закроет _live_open
            return
        try:
            await self.balance.live_close(bet.live_id, bet.amount, bet.win_amount or 0.0)
        except Exception:
            log.exception("live_close")
