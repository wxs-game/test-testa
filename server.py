"""
WXS Game — сервер Краша (aiohttp + WebSocket).

Запуск:  python server.py      (настройки — в .env, см. .env.example)
Клиент подключается к  wss://<домен>/ws  — адрес прописывается в script.js (CRASH_SERVER_URL).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

from engine import (Balance, BalanceError, CrashEngine, DemoBalance, GameError,
                    InsufficientFunds, Journal, Player, now_ms)

log = logging.getLogger("crash.server")


# ---- Настройки --------------------------------------------------------------
def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self) -> None:
        env = os.environ.get
        self.bot_token = env("BOT_TOKEN", "")
        self.supabase_url = env("SUPABASE_URL", "")
        self.supabase_key = env("SUPABASE_SERVICE_KEY", "")
        self.host = env("HOST", "127.0.0.1")
        self.port = int(env("PORT", "8080"))
        self.db_path = env("DB_PATH", "crash.sqlite3")
        self.max_bet = float(env("MAX_BET", "0") or 0)           # 0 = без лимита
        self.allowed_origins = [o.strip() for o in env("ALLOWED_ORIGINS", "").split(",") if o.strip()]
        self.initdata_max_age = int(env("INITDATA_MAX_AGE", str(24 * 3600)))
        self.dev_mode = env("DEV_MODE", "0") == "1"


# ---- Проверка подписи Telegram initData -------------------------------------
class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int) -> dict:
    """https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app"""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            raise AuthError("bad signature")
        if time.time() - int(parsed.get("auth_date", "0")) > max_age:
            raise AuthError("initData expired")
        user = json.loads(parsed["user"])
        int(user["id"])
        return user
    except AuthError:
        raise
    except Exception as e:
        raise AuthError(f"bad initData: {e}")


# ---- Баланс в Supabase (те же RPC, что вызывал script.js) -------------------
class SupabaseBalance(Balance):
    """Ходит в Supabase с service_role-ключом (он только на сервере!).
    Использует ТЕ ЖЕ функции place_bet / resolve_win, что и клиент раньше, — поэтому
    общий баланс, оборот, макс. коэффициент и остальные игры продолжают работать как есть."""

    def __init__(self, url: str, service_key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=10))

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def _request(self, method: str, path: str, **kw) -> Any:
        try:
            async with self.session.request(method, self.base + path, **kw) as r:
                text = await r.text()
                if r.status >= 400:
                    raise BalanceError(f"{method} {path}: {r.status} {text[:300]}", r.status)
                return json.loads(text) if text else None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise BalanceError(f"{method} {path}: {e!r}")

    async def profile(self, tg_id: int) -> Optional[dict]:
        rows = await self._request("GET", "/wxs_game", params={
            "telegram_id": f"eq.{tg_id}", "select": "balance,nickname,username,photo_url", "limit": "1"})
        if not rows:
            return None
        row = rows[0]
        return {"balance": float(row["balance"]),
                "name": row.get("nickname") or row.get("username") or "Игрок",
                "avatar": row.get("photo_url") or None}

    async def place_bet(self, tg_id: int, amount: float, game: str) -> float:
        try:
            data = await self._request("POST", "/rpc/place_bet", json={
                "p_telegram_id": tg_id, "p_amount": amount, "p_game": game})
        except BalanceError as e:
            if e.status and 400 <= e.status < 500:     # функция отказала — скорее всего не хватает денег
                p = await self.profile(tg_id)
                if p and p["balance"] < amount:
                    raise InsufficientFunds() from e
            raise
        return float(data)

    async def credit_win(self, tg_id: int, amount: float, mult: Optional[float]) -> float:
        data = await self._request("POST", "/rpc/resolve_win", json={
            "p_telegram_id": tg_id, "p_win_amount": amount, "p_multiplier": mult})
        return float(data)

    async def live_open(self, tg_id: int, name: str, amount: float, game: str) -> Any:
        rows = await self._request("POST", "/live_bets", params={"select": "id"},
                                   headers={"Prefer": "return=representation"},
                                   json={"telegram_id": tg_id, "name": name, "amount": amount, "game": game})
        return rows[0]["id"] if rows else None

    async def live_close(self, live_id: Any, amount: float, win: float) -> None:
        await self._request("PATCH", "/live_bets", params={"id": f"eq.{live_id}"}, json={
            "win_amount": win,
            "multiplier": round(win / amount, 2) if amount > 0 else 0,
            "result": "win" if win > 0 else "loss",
            "resolved_at": datetime.now(timezone.utc).isoformat()})


# ---- Подключения ------------------------------------------------------------
class Session:
    def __init__(self, ws: web.WebSocketResponse, player: Player) -> None:
        self.ws, self.player = ws, player
        self._window_start, self._count = time.monotonic(), 0

    async def send_str(self, payload: str) -> None:
        if self.ws.closed:
            return
        try:
            await asyncio.wait_for(self.ws.send_str(payload), 3)
        except Exception:               # зависший/мёртвый клиент не должен тормозить остальных
            await self.ws.close()

    async def send(self, obj: dict) -> None:
        await self.send_str(json.dumps(obj, ensure_ascii=False))

    def too_fast(self) -> bool:
        now = time.monotonic()
        if now - self._window_start > 1:
            self._window_start, self._count = now, 0
        self._count += 1
        return self._count > 20          # >20 сообщений в секунду — не человек


def spawn(app: web.Application, coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    app["bg"].add(t)
    t.add_done_callback(app["bg"].discard)


def make_broadcaster(app: web.Application):
    state = {"scheduled": False}

    async def flush() -> None:
        payload = json.dumps(app["engine"].snapshot(), ensure_ascii=False)
        sessions = list(app["sessions"])
        if sessions:
            await asyncio.gather(*(s.send_str(payload) for s in sessions), return_exceptions=True)

    def fire() -> None:
        state["scheduled"] = False
        spawn(app, flush())

    def on_change(immediate: bool = False) -> None:
        if immediate:
            spawn(app, flush())
        elif not state["scheduled"]:     # склеиваем пачки событий (ставки/кэшауты) в один кадр
            state["scheduled"] = True
            asyncio.get_running_loop().call_later(0.08, fire)

    return on_change


async def push_balance(app: web.Application, tg_id: int, balance: float, skip: Optional[Session] = None) -> None:
    for s in list(app["sessions"]):
        if s.player.id == tg_id and s is not skip:
            await s.send({"type": "balance", "balance": balance})


async def handle_action(app: web.Application, sess: Session, kind: str, msg: dict) -> None:
    """Выполняется отдельной задачей: обрыв соединения не должен прервать списание/зачисление."""
    engine: CrashEngine = app["engine"]
    reply: dict = {"type": "result", "id": msg.get("id")}
    try:
        res = await (engine.place_bet(sess.player, msg.get("amount")) if kind == "bet"
                     else engine.cash_out(sess.player))
        reply.update(ok=True, **res)
    except GameError as e:
        reply.update(ok=False, code=e.code, message=e.message)
    except Exception:
        log.exception("action %s failed", kind)
        reply.update(ok=False, code="server", message="Ошибка сервера, попробуйте ещё раз")
    await sess.send(reply)
    if reply.get("ok") and reply.get("balance") is not None:
        await push_balance(app, sess.player.id, reply["balance"], skip=sess)


# ---- WebSocket --------------------------------------------------------------
async def ws_handler(request: web.Request) -> web.StreamResponse:
    app, cfg = request.app, request.app["cfg"]
    origin = request.headers.get("Origin")
    if cfg.allowed_origins and origin and origin not in cfg.allowed_origins:
        return web.Response(status=403, text="origin not allowed")

    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=8192)
    await ws.prepare(request)
    sess: Optional[Session] = None
    try:
        # 1) авторизация: первое сообщение обязано быть {"type":"auth","initData":"..."}
        first = await asyncio.wait_for(ws.receive(), 10)
        if first.type != aiohttp.WSMsgType.TEXT:
            return ws
        data = json.loads(first.data)
        if data.get("type") != "auth":
            raise AuthError("auth expected")
        init_data = str(data.get("initData") or "")
        if cfg.dev_mode:                        # только для локальных тестов!
            parsed = dict(parse_qsl(init_data))
            user = json.loads(parsed.get("user") or init_data)
        else:
            user = validate_init_data(init_data, cfg.bot_token, cfg.initdata_max_age)
        tg_id = int(user["id"])
        profile = await app["balance"].profile(tg_id)
        if not profile:
            await ws.send_json({"type": "error", "code": "no_player",
                                "message": "Профиль не найден — перезапустите приложение."})
            return ws
        sess = Session(ws, Player(tg_id, profile["name"], profile["avatar"] or user.get("photo_url")))
        app["sessions"].add(sess)
        await sess.send({"type": "auth_ok", "balance": profile["balance"], "server_time": now_ms()})
        await sess.send(app["engine"].snapshot())

        # 2) основной цикл
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            if sess.too_fast():
                await ws.close(code=1008, message=b"rate limit")
                break
            try:
                msg = json.loads(raw.data)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "ping":
                await sess.send({"type": "pong", "t": msg.get("t"), "server_time": now_ms()})
            elif kind in ("bet", "cashout"):
                spawn(app, handle_action(app, sess, kind, msg))
    except (AuthError, ValueError, KeyError) as e:
        log.info("auth failed: %s", e)
        if not ws.closed:
            await ws.send_json({"type": "error", "code": "auth",
                                "message": "Не удалось подтвердить Telegram-сессию. Перезапустите приложение."})
    except asyncio.TimeoutError:
        pass
    except Exception:
        log.exception("ws handler failed")
        if not ws.closed:
            await ws.send_json({"type": "error", "code": "server",
                                "message": "Сервер временно недоступен, попробуйте позже."})
    finally:
        if sess:
            app["sessions"].discard(sess)
        if not ws.closed:
            await ws.close()
    return ws


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "time": now_ms()})


# ---- Запуск -----------------------------------------------------------------
async def on_startup(app: web.Application) -> None:
    cfg: Config = app["cfg"]
    if cfg.supabase_url and cfg.supabase_key:
        balance: Balance = SupabaseBalance(cfg.supabase_url, cfg.supabase_key)
        await balance.start()
    else:
        log.warning("SUPABASE_URL/SUPABASE_SERVICE_KEY не заданы — баланс в памяти (DemoBalance)")
        balance = DemoBalance()
    if cfg.dev_mode:
        log.warning("DEV_MODE=1: подпись Telegram НЕ проверяется. Не включайте это на боевом сервере!")
    elif not cfg.bot_token:
        raise SystemExit("Задайте BOT_TOKEN (токен бота, из которого открывается мини-апп) или DEV_MODE=1")

    engine = CrashEngine(balance, Journal(cfg.db_path), max_bet=cfg.max_bet)
    engine.on_change = make_broadcaster(app)
    app["balance"], app["engine"] = balance, engine
    app["engine_task"] = asyncio.create_task(engine.run())


async def on_cleanup(app: web.Application) -> None:
    app["engine_task"].cancel()
    await asyncio.gather(app["engine_task"], return_exceptions=True)
    for s in list(app["sessions"]):
        await s.ws.close()
    if hasattr(app["balance"], "close"):
        await app["balance"].close()


def create_app(cfg: Config) -> web.Application:
    app = web.Application()
    app["cfg"], app["sessions"], app["bg"] = cfg, set(), set()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    web.run_app(create_app(cfg), host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
