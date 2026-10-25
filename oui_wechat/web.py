"""Authenticated HTTP management of a single adapter process."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from aiohttp import web
from open_webui_weixin.owui import OwuiError

from . import __version__
from .auth import AdminAuth
from .config import ConfigStore
from .logs import LogBuffer
from .service import ServiceManager

CONFIG = web.AppKey("config", ConfigStore)
AUTH = web.AppKey("auth", AdminAuth)
MANAGER = web.AppKey("manager", ServiceManager)
LOGS = web.AppKey("logs", LogBuffer)
LOCK = web.AppKey("directory_lock", object)
COOKIE = "oui_session"
STATIC = Path(__file__).with_name("static")
PUBLIC = {"/api/auth/state", "/api/auth/setup", "/api/auth/login", "/health"}
log = logging.getLogger(__name__)


@web.middleware
async def security(request, handler):
    try:
        if request.path.startswith("/api/"):
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("X-OUI-Request") != "1":
                    raise web.HTTPForbidden(text="无效的管理请求")
                origin = request.headers.get("Origin")
                if origin and urlsplit(origin).netloc != request.host:
                    raise web.HTTPForbidden(text="请求来源不匹配")
            if request.path not in PUBLIC and not request.app[AUTH].valid(request.cookies.get(COOKIE, "")):
                raise web.HTTPUnauthorized(text="请先登录管理页")
        response = await handler(request)
    except ValueError as exc:
        response = web.json_response({"error": str(exc)}, status=400)
    except OwuiError as exc:
        message = (
            "Open WebUI 账号或密码不正确"
            if exc.status_code in (400, 401, 403) else "Open WebUI 请求失败，请检查连接和账号权限"
        )
        response = web.json_response({"error": message}, status=400)
    except httpx.HTTPError:
        response = web.json_response({"error": "无法连接 Open WebUI"}, status=502)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.text}, status=exc.status)
    except Exception:
        log.exception("管理请求处理失败")
        response = web.json_response({"error": "操作失败，请查看运行日志"}, status=500)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def payload(request) -> dict:
    try:
        data = await request.json() if request.can_read_body else {}
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("请求数据格式无效") from exc
    if not isinstance(data, dict):
        raise ValueError("请求数据必须是对象")
    return data


def session_response(request, token: str) -> web.Response:
    response = web.json_response({"authenticated": True})
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="Strict", max_age=12 * 3600,
        secure=request.secure or request.headers.get("X-Forwarded-Proto") == "https",
    )
    return response


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


async def auth_state(request):
    auth = request.app[AUTH]
    return web.json_response({
        "initialized": auth.initialized,
        "authenticated": auth.valid(request.cookies.get(COOKIE, "")),
        "version": __version__,
    })


async def auth_setup(request):
    data = await payload(request)
    token = await request.app[AUTH].setup(data.get("password"))
    return session_response(request, token)


async def auth_login(request):
    data = await payload(request)
    token = await request.app[AUTH].login(data.get("password"), request.remote or "unknown")
    return session_response(request, token)


async def auth_logout(request):
    request.app[AUTH].sessions.pop(request.cookies.get(COOKIE, ""), None)
    response = web.json_response({"authenticated": False})
    response.del_cookie(COOKIE)
    return response


async def auth_change(request):
    data = await payload(request)
    token = await request.app[AUTH].change_password(data.get("current"), data.get("password"))
    return session_response(request, token)


async def status(request):
    manager = request.app[MANAGER]
    accounts = manager.accounts()
    return web.json_response({
        "version": __version__, "enabled": manager.enabled, "running": manager.running,
        "error": manager.last_error, "accounts": len(accounts),
        "connected_accounts": sum(item["connected"] for item in accounts),
        "bound_accounts": sum(item["bound"] for item in accounts),
        "owui": await manager.health(force=request.query.get("refresh") == "1"),
    })


async def settings_get(request):
    return web.json_response(request.app[CONFIG].public())


async def settings_save(request):
    data = await payload(request)
    if any(not isinstance(data.get(section, {}), dict)
           for section in ("owui", "display", "capabilities", "reply")):
        raise ValueError("设置分组格式无效")
    await request.app[MANAGER].apply_settings(data)
    return web.json_response(request.app[CONFIG].public())


async def service_control(request):
    action = request.match_info["action"]
    manager = request.app[MANAGER]
    if action == "start":
        await manager.start()
    elif action == "stop":
        await manager.stop()
    else:
        raise web.HTTPNotFound()
    return web.json_response({"enabled": manager.enabled, "running": manager.running})


async def accounts_get(request):
    return web.json_response({"accounts": request.app[MANAGER].accounts()})


async def account_action(request):
    manager = request.app[MANAGER]
    account_id = request.match_info["account_id"]
    action = request.match_info["action"]
    data = await payload(request)
    if action == "bind":
        await manager.bind(account_id, data.get("email"), data.get("password"))
    elif action == "unbind":
        await manager.unbind(account_id)
    elif action == "remove":
        await manager.remove(account_id)
    elif action == "rename":
        manager.rename(account_id, data.get("name"))
    elif action == "model":
        await manager.set_model(account_id, data.get("model_id"))
    elif action == "new-chat":
        await manager.new_chat(account_id)
    elif action == "verify":
        return web.json_response(await manager.verify_binding(account_id))
    else:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


async def models_get(request):
    return web.json_response({
        "models": await request.app[MANAGER].models(request.match_info["account_id"])
    })


async def pairing_add(request):
    job = await request.app[MANAGER].add_pairing()
    return web.json_response(job.public(), status=201)


def pairing(request):
    job = request.app[MANAGER].jobs.get(request.match_info["job_id"])
    if not job:
        raise web.HTTPNotFound(text="扫码任务不存在")
    return job


async def pairing_get(request):
    return web.json_response(pairing(request).public())


async def pairing_qr(request):
    job = pairing(request)
    if not job.image:
        raise web.HTTPNotFound(text="二维码尚未生成或已完成授权")
    return web.Response(body=job.image, content_type="image/png", headers={"Cache-Control": "no-store"})


async def pairing_cancel(request):
    pairing(request)
    await request.app[MANAGER].cancel_pairing(request.match_info["job_id"])
    return web.json_response({"ok": True})


async def pairing_code(request):
    job = pairing(request)
    data = await payload(request)
    code = data.get("code")
    if not isinstance(code, str) or not code.isdigit() or not 4 <= len(code) <= 12:
        raise ValueError("请输入微信显示的数字配对码")
    if job.status != "need_verifycode" or job.code.full():
        raise ValueError("当前不需要配对码")
    job.code.put_nowait(code)
    return web.json_response({"ok": True})


async def logs_get(request):
    after = int(request.query.get("after", "0"))
    entries = [item for item in request.app[LOGS].entries if item["id"] > after]
    return web.json_response({"entries": entries})


async def health(request):
    return web.json_response({"status": "ok", "version": __version__})


def create_app(directory: Path, *, manager_factory=ServiceManager) -> web.Application:
    config = ConfigStore(directory)
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app[CONFIG] = config
    app[AUTH] = AdminAuth(config.directory / "admin.json")
    app[MANAGER] = manager_factory(config)
    app[LOGS] = LogBuffer()

    async def startup(application):
        if os.name == "posix":
            import fcntl

            handle = (config.directory / "service.lock").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise RuntimeError("已有管理服务使用此数据目录") from exc
            application[LOCK] = handle
        logging.getLogger().addHandler(application[LOGS])
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        await application[MANAGER].reconcile()

    async def cleanup(application):
        await application[MANAGER].close()
        logging.getLogger().removeHandler(application[LOGS])
        if LOCK in application:
            application[LOCK].close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.get("/api/auth/state", auth_state), web.post("/api/auth/setup", auth_setup),
        web.post("/api/auth/login", auth_login), web.post("/api/auth/logout", auth_logout),
        web.post("/api/auth/password", auth_change), web.get("/api/status", status),
        web.get("/api/settings", settings_get), web.put("/api/settings", settings_save),
        web.post("/api/service/{action}", service_control),
        web.get("/api/accounts", accounts_get),
        web.get("/api/accounts/{account_id}/models", models_get),
        web.post("/api/accounts/{account_id}/{action}", account_action),
        web.post("/api/pairings", pairing_add), web.get("/api/pairings/{job_id}", pairing_get),
        web.get("/api/pairings/{job_id}/qr", pairing_qr),
        web.delete("/api/pairings/{job_id}", pairing_cancel),
        web.post("/api/pairings/{job_id}/code", pairing_code), web.get("/api/logs", logs_get),
    ])
    app.router.add_static("/static/", STATIC, show_index=False)
    return app
