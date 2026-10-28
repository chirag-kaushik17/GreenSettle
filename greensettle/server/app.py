"""The GreenSettle server: HTTP API + scheduler tick + ledger + simulated clock.

PULL model: the worker on the laptop asks for work (POST /agent/claim) and
posts results back. The server never connects to the worker, so the laptop
can sit behind NAT.

Who may call what:
  X-API-Key      a user: submit, list, read, cancel their OWN jobs; GET /me
  X-Agent-Token  the worker: /agent/claim, /agent/result, /agent/telemetry
  X-Admin-Token  the admin: /admin/*

TRUST MODEL: the server trusts the energy numbers the worker reports.
Measurement attestation is out of scope; a dishonest worker could lie.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from greensettle.intensity.provider import SimulatedCurve
from greensettle.intensity.timeline import ActualProvider, ForecastProvider, mean_intensity
from greensettle.ledger import db as ledger_db
from greensettle.ledger.config import tier_for
from greensettle.ledger.credits import MICRO_PER_CREDIT
from greensettle.ledger.settle import SettleOutcome, settle_job
from greensettle.scheduler import queue
from greensettle.scheduler.planner import ChooseFn
from greensettle.scheduler.slots import choose_slot
from greensettle.server import store
from greensettle.server.clock import Clock, RealTimeFn
from greensettle.server.config import ServerConfig

log = logging.getLogger("greensettle.server")

DASHBOARD_FILE = Path(__file__).resolve().parent / "static" / "dashboard.html"
# The dashboard is one self-contained file: no CDN, no web fonts, no external
# requests. This policy makes the browser enforce that.
DASHBOARD_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"  # user ids: lowercase, short, no spaces
AGENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


def credits(micro: int) -> float:
    return micro / MICRO_PER_CREDIT


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Request models: strict (no type coercion, no NaN/inf), unknown fields rejected
# --------------------------------------------------------------------------
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class NewUser(Strict):
    user_id: str = Field(pattern=ID_PATTERN)


class AllowanceIn(Strict):
    user_id: str = Field(pattern=ID_PATTERN)
    amount: float | None = Field(default=None, gt=0, le=1_000_000)
    period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class IntensityIn(Strict):
    state: Literal["green", "dirty", "off"]


class JobIn(Strict):
    job_type: str = Field(max_length=32)
    params: dict[str, Any]  # checked by the catalog: integers only, hard ranges
    est_duration_s: float = Field(gt=0, le=120)  # REAL seconds
    max_delay_h: float = Field(ge=0, le=queue.MAX_DELAY_H)


class ClaimIn(Strict):
    agent_id: str = Field(pattern=AGENT_PATTERN)


class ResultIn(Strict):
    job_id: str = Field(max_length=64)
    agent_id: str = Field(pattern=AGENT_PATTERN)
    exit_code: int
    started_real: float = Field(ge=0)  # REAL epoch seconds
    ended_real: float = Field(ge=0)
    duration_s: float = Field(ge=0)
    energy_kwh_job: float = Field(ge=0)
    energy_unc_kwh: float = Field(ge=0)
    cpu_share: float = Field(ge=0, le=1)
    job_cpu_s: float = Field(ge=0)
    system_busy_cpu_s: float = Field(ge=0)
    samples: int = Field(ge=0)
    scope: str = Field(max_length=64)

    @model_validator(mode="after")
    def _ends_after_start(self) -> "ResultIn":
        if self.ended_real < self.started_real:
            raise ValueError("ended_real must not be before started_real")
        return self


class TelemetrySample(Strict):
    ts_real: float = Field(ge=0)
    watts: float = Field(ge=0, le=1000)


class TelemetryIn(Strict):
    agent_id: str = Field(pattern=AGENT_PATTERN)
    samples: list[TelemetrySample] = Field(max_length=120)


# --------------------------------------------------------------------------
# Body size limit (pure ASGI middleware, so it also catches bodies without
# a Content-Length header)
# --------------------------------------------------------------------------
class BodySizeLimit:
    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
            return
        seen = 0

        async def limited_receive() -> dict:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise HTTPException(status_code=413, detail="request body too large")
            return message

        await self.app(scope, limited_receive, send)


# --------------------------------------------------------------------------
# Server state shared by all requests
# --------------------------------------------------------------------------
class ServerState:
    def __init__(self, config: ServerConfig, real_time: RealTimeFn, choose: ChooseFn) -> None:
        self.config = config
        self.real_time = real_time
        self.choose = choose
        self.curve = SimulatedCurve(seed=config.curve_seed, tz=ZoneInfo(config.curve_tz))
        self.forecast = ForecastProvider(self.curve, config.forecast_errors)
        conn = self.connect()
        try:
            self.clock: Clock = store.load_clock(conn, config.time_scale, real_time, config.sim_start)
        finally:
            conn.close()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def connect(self) -> sqlite3.Connection:
        # check_same_thread=False: FastAPI may run a request's dependency and
        # its endpoint on different threads. Each request still gets its own
        # connection, so no connection is ever used by two requests at once.
        return store.open_db(self.config.db_path, check_same_thread=False)

    def actual(self, conn: sqlite3.Connection) -> ActualProvider:
        return ActualProvider(self.curve, store.load_timeline(conn))

    # ---- scheduler tick -------------------------------------------------
    def tick(self) -> queue.TickReport:
        conn = self.connect()
        try:
            return queue.tick(conn, self.clock.now(), self.real_time(), self.config.ledger)
        finally:
            conn.close()

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.config.tick_interval_s):
            try:
                report = self.tick()
                if report.lease_changes:
                    log.info("leases: %s", report.lease_changes)
            except Exception:  # keep ticking; one bad tick must not stop the scheduler
                log.exception("scheduler tick failed")

    def start(self) -> None:
        if self.config.run_tick_thread and self._thread is None:
            self._thread = threading.Thread(target=self._tick_loop, name="greensettle-tick", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def create_app(config: ServerConfig, real_time: RealTimeFn = time.time, choose: ChooseFn = choose_slot) -> FastAPI:
    """Build the app. Tests pass a fake real_time and (until it exists) a fake choose."""
    state = ServerState(config, real_time, choose)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.start()
        yield
        state.stop()

    app = FastAPI(title="GreenSettle", version="3", lifespan=lifespan)
    app.state.gs = state
    app.add_middleware(BodySizeLimit, max_bytes=config.max_body_bytes)

    # ---- dependencies -----------------------------------------------------
    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = state.connect()
        try:
            yield conn
        finally:
            conn.close()

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if x_admin_token is None or not hmac.compare_digest(x_admin_token, config.admin_token):
            raise HTTPException(status_code=401, detail="admin token missing or wrong")

    def require_agent(x_agent_token: str | None = Header(default=None)) -> None:
        if x_agent_token is None or not hmac.compare_digest(x_agent_token, config.agent_token):
            raise HTTPException(status_code=401, detail="agent token missing or wrong")

    def require_user(
        x_api_key: str | None = Header(default=None), conn: sqlite3.Connection = Depends(get_conn)
    ) -> str:
        """The user whose API key this is. Only the SHA-256 of keys is stored."""
        if not x_api_key:
            raise HTTPException(status_code=401, detail="X-API-Key missing")
        presented = hash_key(x_api_key)
        for row in conn.execute("SELECT user_id, api_key_hash FROM users WHERE api_key_hash IS NOT NULL"):
            if hmac.compare_digest(presented, row["api_key_hash"]):
                return row["user_id"]
        raise HTTPException(status_code=401, detail="API key not recognised")

    def require_user_or_admin(
        x_api_key: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> None:
        if x_admin_token is not None and hmac.compare_digest(x_admin_token, config.admin_token):
            return
        require_user(x_api_key, conn)

    # ---- error mapping ----------------------------------------------------
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 that says WHERE and WHY, but never echoes the input back.

        (FastAPI's default echoes it, which crashes on NaN, since NaN is not
        valid JSON, and would reflect whatever a client sent.)
        """
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "invalid")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(NotImplementedError)
    async def not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse({"detail": f"NotImplementedError: {exc}"}, status_code=501)

    # ---- helpers ----------------------------------------------------------
    def settlement_json(out: SettleOutcome) -> dict[str, Any]:
        return {
            "job_id": out.job_id, "zone": out.zone, "settled_at": out.settled_at,
            "saved_g": out.saved_g, "saved_unc_g": out.saved_unc_g,
            "fee": credits(out.fee_micro), "rebate": credits(out.rebate_micro),
            "surcharge": credits(out.surcharge_micro), "net": credits(out.net_micro),
            "balance_after": credits(out.balance_micro_after), "tier_after": out.tier_after,
            "params": out.params, "already_settled": out.already_settled,
        }

    def stored_settlement(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM settlements WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return {
            "zone": row["zone"], "settled_at": row["settled_at"], "saved_g": row["saved_g"],
            "saved_unc_g": row["saved_unc_g"], "i_submit": row["i_submit"], "i_run": row["i_run"],
            "energy_kwh": row["energy_kwh"], "fee": credits(row["fee_micro"]),
            "rebate": credits(row["rebate_micro"]), "surcharge": credits(row["surcharge_micro"]),
            "net": credits(row["net_micro"]), "params": json.loads(row["params_json"]),
        }

    # ======================================================================
    # Endpoints
    # ======================================================================
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "sim_now": state.clock.now().isoformat()}

    # ---- admin --------------------------------------------------------------
    @app.post("/admin/users", status_code=201, dependencies=[Depends(require_admin)])
    def admin_create_user(body: NewUser, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        api_key = secrets.token_hex(32)  # 32 random bytes, shown ONCE
        try:
            ledger_db.create_user(conn, body.user_id, state.clock.now(), api_key_hash=hash_key(api_key))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"user_id": body.user_id, "api_key": api_key,
                "note": "store this key now: the server keeps only its hash and cannot show it again"}

    @app.post("/admin/allowance", dependencies=[Depends(require_admin)])
    def admin_allowance(body: AllowanceIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        try:
            res = ledger_db.grant_allowance(conn, body.user_id, state.clock.now(), body.amount, body.period,
                                            config.ledger)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "unknown user" in str(exc) else 422, detail=str(exc)) from exc
        return {"granted": res.granted, "period": res.period, "message": res.message,
                "amount": credits(res.amount_micro), "decay": credits(res.decay_micro),
                "balance": credits(ledger_db.balance_micro(conn, body.user_id))}

    @app.post("/admin/intensity", dependencies=[Depends(require_admin)])
    def admin_intensity(body: IntensityIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        """Append a demo override starting NOW (sim time). The past is never changed."""
        now = state.clock.now()
        try:
            store.append_override(conn, now, body.state)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"from_sim": now.isoformat(), "state": body.state}

    @app.get("/admin/state", dependencies=[Depends(require_admin)])
    def admin_state(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        now = state.clock.now()
        jobs = conn.execute(
            "SELECT job_id, user_id, job_type, status, submitted_at, planned_start, latest_start, "
            "declared_window_h, est_duration_s, claimed_by, lease_expires_real, plan_json "
            "FROM jobs WHERE status IN ('queued', 'running') ORDER BY planned_start"
        ).fetchall()
        settlements = conn.execute(
            "SELECT job_id, user_id, zone, fee_micro, rebate_micro, surcharge_micro, net_micro, settled_at "
            "FROM settlements ORDER BY settled_at DESC, job_id DESC LIMIT 20"
        ).fetchall()
        users = []
        for row in conn.execute("SELECT user_id, last_decay_at FROM users ORDER BY user_id"):
            uid = row["user_id"]
            bal = ledger_db.balance_micro(conn, uid)
            decayed = conn.execute("SELECT COALESCE(SUM(amount_micro), 0) FROM ledger "
                                   "WHERE user_id = ? AND kind = 'decay'", (uid,)).fetchone()[0]
            series = conn.execute(
                "SELECT ts, balance_micro FROM (SELECT entry_id, ts, balance_micro FROM ledger_with_balance "
                "WHERE user_id = ? ORDER BY entry_id DESC LIMIT 60) ORDER BY entry_id", (uid,)).fetchall()
            users.append({"user_id": uid, "balance": credits(bal),
                          "tier": tier_for(credits(bal), config.ledger.tiers).name,
                          "decay_total": credits(-decayed), "last_decay_at": row["last_decay_at"],
                          "balance_series": [[r["ts"], credits(r["balance_micro"])] for r in series]})

        def job_row(j: sqlite3.Row) -> dict[str, Any]:
            out = {k: j[k] for k in j.keys() if k != "plan_json"}
            out["max_delay_h"] = out.pop("declared_window_h")
            out["plan"] = json.loads(j["plan_json"]) if j["plan_json"] else None
            return out

        return {
            "sim_now": now.isoformat(),
            "time_scale": state.clock.scale,
            "intensity_now": state.actual(conn).get(now)._asdict(),
            "queue": [job_row(j) | {"ready": store.parse_ts(j["planned_start"]) <= now}
                      for j in jobs if j["status"] == "queued"],
            "running": [job_row(j) for j in jobs if j["status"] == "running"],
            "settlements": [{"job_id": s["job_id"], "user_id": s["user_id"], "zone": s["zone"],
                             "fee": credits(s["fee_micro"]), "rebate": credits(s["rebate_micro"]),
                             "surcharge": credits(s["surcharge_micro"]), "net": credits(s["net_micro"]),
                             "settled_at": s["settled_at"]} for s in settlements],
            "users": users,
        }

    @app.get("/admin/history", dependencies=[Depends(require_admin)])
    def admin_history(n: int = Query(default=200, ge=1, le=1000),
                      conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
        """Settled jobs, newest first, with every number the settlement used,
        the plan, and the sim run window. Read-only."""
        rows = conn.execute(
            """SELECT s.*, j.job_type, j.params_json AS job_params_json, j.submitted_at, j.planned_start, j.plan_json,
                      j.started_real, j.ended_real, j.declared_window_h
               FROM settlements s JOIN jobs j ON j.job_id = s.job_id
               ORDER BY s.settled_at DESC, s.job_id DESC LIMIT ?""", (n,)).fetchall()
        out = []
        for r in rows:
            started, ended = r["started_real"], r["ended_real"]
            out.append({
                "job_id": r["job_id"], "user_id": r["user_id"], "job_type": r["job_type"],
                "params": json.loads(r["job_params_json"]) if r["job_params_json"] else None,
                "submitted_at": r["submitted_at"], "planned_start": r["planned_start"],
                "max_delay_h": r["declared_window_h"],
                "run_start_sim": state.clock.real_to_sim(started).isoformat() if started is not None else None,
                "run_end_sim": state.clock.real_to_sim(ended).isoformat() if ended is not None else None,
                "settled_at": r["settled_at"], "zone": r["zone"],
                "energy_kwh": r["energy_kwh"], "energy_unc_kwh": r["energy_unc_kwh"],
                "i_submit": r["i_submit"], "i_submit_unc": r["i_submit_unc"],
                "i_run": r["i_run"], "i_run_unc": r["i_run_unc"],
                "saved_g": r["saved_g"], "saved_unc_g": r["saved_unc_g"],
                "fee": credits(r["fee_micro"]), "rebate": credits(r["rebate_micro"]),
                "surcharge": credits(r["surcharge_micro"]), "net": credits(r["net_micro"]),
                "settlement_params": json.loads(r["params_json"]),  # p, rho, gamma, k, sigma used
                "plan": json.loads(r["plan_json"]) if r["plan_json"] else None,
            })
        return out

    @app.get("/admin/intensity_curve", dependencies=[Depends(require_admin)])
    def admin_intensity_curve(hours: float = Query(default=24, ge=1, le=72),
                              conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        """Actual and forecast intensity around now, for the dashboard. Read-only.

        Window: from hours/4 before now to 3*hours/4 after (the future matters
        for scheduling). ACTUAL = simulated curve + override timeline; for future
        times it assumes no further overrides. FORECAST = issued now, no
        overrides, simulated error growing with lead time (future points only).
        """
        now = state.clock.now()
        start, end = now - timedelta(hours=hours / 4), now + timedelta(hours=3 * hours / 4)
        step = timedelta(minutes=15)
        actual = state.actual(conn)
        actual_series, forecast_series = [], []
        t = start
        while t <= end:
            a = actual.get(t)
            actual_series.append({"t": t.isoformat(), "g": a.g_per_kwh, "unc": a.unc_g_per_kwh})
            if t >= now:
                g, unc = state.forecast.point(t, issued_at=now)
                forecast_series.append({"t": t.isoformat(), "g": g, "unc": unc})
            t += step
        entries = actual.timeline.entries
        overrides = []
        for i, (frm, st) in enumerate(entries):
            until = entries[i + 1][0] if i + 1 < len(entries) else end
            if st in ("green", "dirty") and until > start and frm < end:
                overrides.append({"from": max(frm, start).isoformat(), "to": min(until, end).isoformat(),
                                  "state": st})
        return {"now": now.isoformat(), "from": start.isoformat(), "to": end.isoformat(),
                "simulated": True, "actual": actual_series, "forecast": forecast_series, "overrides": overrides,
                "note": "simulated grid curve; forecast error is simulated (placeholder model)"}

    # ---- dashboard (static page; its data calls need the admin token) -----------
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_FILE.read_text(encoding="utf-8"), headers=DASHBOARD_HEADERS)

    # ---- users: jobs and balance ---------------------------------------------
    @app.post("/jobs", status_code=201)
    def submit(body: JobIn, user: str = Depends(require_user),
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        try:
            res = queue.submit_job(
                conn, user_id=user, job_type=body.job_type, params=body.params,
                est_duration_s=body.est_duration_s, max_delay_h=body.max_delay_h,
                sim_now=state.clock.now(), time_scale=state.clock.scale,
                forecast=state.forecast, actual=state.actual(conn), sched_params=config.scheduler,
                max_queued=config.max_queued_per_user, choose=state.choose,
            )
        except queue.QueueFullError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except queue.SubmitError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        c = res.plan.choice
        return {
            "job_id": res.job_id, "planned_start": c.start.isoformat(),
            "latest_start": res.plan.latest_start.isoformat(), "deferred": c.deferred, "reason": c.reason,
            "forecast_mean": c.forecast_mean, "forecast_unc": c.forecast_unc,
            "forecast_now_mean": c.forecast_now_mean, "expected_saving": c.expected_saving,
            "est_duration_sim_s": res.plan.est_duration_sim_s,
        }

    @app.get("/jobs")
    def list_jobs(user: str = Depends(require_user), conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        rows = conn.execute(
            "SELECT job_id, job_type, status, submitted_at, planned_start FROM jobs "
            "WHERE user_id = ? ORDER BY rowid DESC LIMIT 50", (user,)
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, user: str = Depends(require_user),
                conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        try:
            job = queue.get_job(conn, job_id, user)
        except queue.JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        job.pop("lease_expires_real", None)
        job["settlement"] = stored_settlement(conn, job_id)
        return job

    @app.delete("/jobs/{job_id}")
    def cancel(job_id: str, user: str = Depends(require_user),
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, str]:
        try:
            queue.cancel_job(conn, job_id, user)
        except queue.JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "status": "cancelled"}

    @app.get("/me")
    def me(user: str = Depends(require_user), conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        bal = ledger_db.balance_micro(conn, user)
        entries = conn.execute(
            "SELECT ts, kind, amount_micro, job_id, period_key, balance_micro FROM ledger_with_balance "
            "WHERE user_id = ? ORDER BY entry_id DESC LIMIT 10", (user,)
        ).fetchall()
        return {
            "user_id": user, "balance": credits(bal), "tier": tier_for(credits(bal), config.ledger.tiers).name,
            "last_decay_at": ledger_db.last_decay_at(conn, user).isoformat(),
            "last_entries": [{"ts": e["ts"], "kind": e["kind"], "amount": credits(e["amount_micro"]),
                              "job_id": e["job_id"], "period": e["period_key"],
                              "balance": credits(e["balance_micro"])} for e in entries],
        }

    # ---- worker (agent) -------------------------------------------------------
    @app.post("/agent/claim", dependencies=[Depends(require_agent)])
    def claim(body: ClaimIn, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        got = queue.claim_job(conn, body.agent_id, state.clock.now(), state.real_time(),
                              max_concurrent_jobs=config.max_concurrent_jobs, config=config.ledger)
        if got is None:
            return Response(status_code=204)
        return {"job_id": got.job_id, "job_type": got.job_type, "params": got.params,
                "est_duration_s": got.est_duration_s, "lease_expires_real": got.lease_expires_real}

    @app.post("/agent/result", dependencies=[Depends(require_agent)])
    def result(body: ResultIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (body.job_id,)).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job["claimed_by"] != body.agent_id:
            raise HTTPException(status_code=409, detail="this agent does not hold the job's lease")

        # Idempotent: a repeated result for a settled job returns the stored settlement.
        if job["status"] == "done":
            stored = stored_settlement(conn, body.job_id)
            return {"status": "done", "settlement": stored, "already_settled": True}
        if job["status"] == "failed":
            return {"status": "failed", "failure_reason": job["failure_reason"]}
        if job["status"] != "running":
            raise HTTPException(status_code=409, detail=f"job is {job['status']}")

        if body.exit_code != 0:
            # Failed jobs are NOT settled: no fee, no rebate, no ledger rows.
            with ledger_db.immediate(conn):
                conn.execute(
                    "UPDATE jobs SET status = 'failed', failure_reason = ?, started_real = ?, ended_real = ?, "
                    "lease_expires_real = NULL WHERE job_id = ? AND status = 'running'",
                    (f"exit code {body.exit_code}", body.started_real, body.ended_real, body.job_id),
                )
            return {"status": "failed", "failure_reason": f"exit code {body.exit_code}"}

        # Real start/end -> sim time; intensities from ACTUAL data (overrides included).
        sim_start, sim_end = state.clock.real_to_sim(body.started_real), state.clock.real_to_sim(body.ended_real)
        actual = state.actual(conn)
        i_run, i_run_unc = mean_intensity(actual, sim_start, sim_end)
        t0 = store.parse_ts(job["submitted_at"])
        # The counterfactual: the SAME job started at submission, using actual data (not the forecast).
        i_submit, i_submit_unc = mean_intensity(actual, t0, t0 + (sim_end - sim_start))
        try:
            out = settle_job(
                conn, job_id=body.job_id, user_id=job["user_id"], now=state.clock.now(),
                energy_kwh=body.energy_kwh_job, energy_unc_kwh=body.energy_unc_kwh,
                i_submit=i_submit, i_submit_unc=i_submit_unc, i_run=i_run, i_run_unc=i_run_unc,
                config=config.ledger,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with ledger_db.immediate(conn):
            conn.execute(
                "UPDATE jobs SET status = 'done', started_real = ?, ended_real = ?, lease_expires_real = NULL "
                "WHERE job_id = ?", (body.started_real, body.ended_real, body.job_id),
            )
        return {"status": "done", "settlement": settlement_json(out), "already_settled": out.already_settled,
                "sim_start": sim_start.isoformat(), "sim_end": sim_end.isoformat()}

    @app.post("/agent/telemetry", dependencies=[Depends(require_agent)])
    def telemetry(body: TelemetryIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, int]:
        with ledger_db.immediate(conn):
            conn.executemany("INSERT INTO telemetry (ts_real, agent_id, watts) VALUES (?, ?, ?)",
                             [(s.ts_real, body.agent_id, s.watts) for s in body.samples])
            conn.execute("DELETE FROM telemetry WHERE rowid NOT IN "
                         "(SELECT rowid FROM telemetry ORDER BY rowid DESC LIMIT ?)", (config.telemetry_keep,))
        return {"stored": len(body.samples)}

    @app.get("/telemetry/latest", dependencies=[Depends(require_user_or_admin)])
    def telemetry_latest(n: int = Query(default=60, ge=1, le=1000),
                         conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
        """Latest samples, newest first. gCO2e/s is computed NOW from the actual
        provider, so a demo override shows up immediately."""
        actual = state.actual(conn)
        rows = conn.execute("SELECT ts_real, agent_id, watts FROM telemetry ORDER BY rowid DESC LIMIT ?",
                            (n,)).fetchall()
        out = []
        for r in rows:
            sim_t = state.clock.real_to_sim(r["ts_real"])
            g = actual.get(sim_t).g_per_kwh
            out.append({"ts_real": r["ts_real"], "sim_time": sim_t.isoformat(), "agent_id": r["agent_id"],
                        "watts": r["watts"], "g_per_kwh": g, "gco2e_per_s": r["watts"] / 3.6e6 * g})
        return out

    return app

