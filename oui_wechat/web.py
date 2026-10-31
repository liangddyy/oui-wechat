"""Authenticated HTTP management of a single adapter process."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from aiohttp import web
from open_webui_weixin.owui import OwuiError

from . import __version__
from .auth import AdminAuth, Principal, RateLimited
from .config import ConfigStore
from .logs import LogBuffer
from .proxy import ProxyPolicy
from .service import ServiceManager

CONFIG = web.AppKey("config", ConfigStore)
AUTH = web.AppKey("auth", AdminAuth)
MANAGER = web.AppKey("manager", ServiceManager)
LOGS = web.AppKey("logs", LogBuffer)
LOCK = web.AppKey("directory_lock", object)
PROXY = web.AppKey("proxy", ProxyPolicy)
PRINCIPAL = getattr(web, "RequestKey", web.AppKey)("principal", Principal)
COOKIE = "oui_session"
STATIC = Path(__file__).with_name("static")
PUBLIC = {
    "/api/auth/state", "/api/auth/setup", "/api/auth/login", "/api/auth/owui", "/health",
}
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
            if request.path not in PUBLIC or request.path == "/api/auth/state":
                principal = await current_principal(request)
                if request.path not in PUBLIC and not principal:
                    raise web.HTTPUnauthorized(text="请先登录")
                if principal:
                    request[PRINCIPAL] = principal
                    if principal.role != "admin" and request.path not in {
                        "/api/auth/state", "/api/auth/logout",
                    }:
                        if not (
                            request.path in {"/api/status", "/api/accounts", "/api/pairings"}
                            or request.path.startswith(("/api/accounts/", "/api/pairings/"))
                        ):
                            raise web.HTTPForbidden(text="此操作需要管理员权限")
        response = await handler(request)
    except RateLimited as exc:
        response = web.json_response(
            {"error": str(exc), "retry_after": exc.retry_after}, status=429,
            headers={"Retry-After": str(exc.retry_after)},
        )
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
        response = web.json_response({"error": "操作失败，请稍后重试或联系管理员"}, status=500)
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


async def current_principal(request) -> Principal | None:
    auth = request.app[AUTH]
    token = request.cookies.get(COOKIE, "")
    principal = auth.principal(token)
    if not principal or principal.role == "admin":
        return principal
    manager = request.app[MANAGER]
    if not manager.user_session_active(principal):
        auth.revoke(token)
        return None
    async with principal.check_lock:
        if principal.checked_at < time.monotonic() - 60:
            try:
                session = await manager.owui.whoami(principal.owui.jwt_token)
                manager.validate_session(session)
                if session.user_id != principal.owui.user_id:
                    raise ValueError("身份已变化")
            except (OwuiError, ValueError):
                auth.revoke(token)
                return None
            principal.checked_at = time.monotonic()
    return principal if manager.user_session_active(principal) else None


async def payload(request) -> dict:
    try:
        data = await request.json() if request.can_read_body else {}
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("请求数据格式无效") from exc
    if not isinstance(data, dict):
        raise ValueError("请求数据必须是对象")
    return data


def session_response(request, token: str) -> web.Response:
    auth = request.app[AUTH]
    auth.revoke(request.cookies.get(COOKIE, ""))
    principal = auth.principal(token)
    response = web.json_response({"authenticated": True, **principal.public()})
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="Strict", max_age=12 * 3600,
        secure=request.app[PROXY].secure(request),
    )
    return response


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


async def auth_state(request):
    auth = request.app[AUTH]
    principal = request.get(PRINCIPAL)
    return web.json_response({
        "initialized": auth.initialized,
        "authenticated": principal is not None,
        "user_login": auth.initialized and request.app[CONFIG].user_login,
        "role": principal.role if principal else None,
        "user": principal.public()["user"] if principal else None,
        "version": __version__,
    })


async def auth_setup(request):
    if request.app[AUTH].initialized:
        raise ValueError("管理密码已经设置")
    data = await payload(request)
    token = await request.app[AUTH].setup(data.get("password"))
    return session_response(request, token)


async def auth_login(request):
    data = await payload(request)
    token = await request.app[AUTH].login(data.get("password"), request.app[PROXY].peer(request))
    return session_response(request, token)


def login_email(data: dict) -> str:
    email = data.get("email")
    password = data.get("password")
    if (
        not isinstance(email, str) or not email.strip() or len(email) > 320
        or not isinstance(password, str) or not 1 <= len(password) <= 1024
    ):
        raise ValueError("请输入有效的 Open WebUI 邮箱和密码")
    return email.strip().lower()


async def auth_owui(request):
    manager = request.app[MANAGER]
    if not request.app[AUTH].initialized or not request.app[CONFIG].user_login:
        raise web.HTTPForbidden(text="管理员尚未开放用户登录")
    data = await payload(request)
    email = login_email(data)
    base_url = manager.config.cfg.owui.base_url
    async with request.app[AUTH].credential_check(request.app[PROXY].peer(request), f"owui:{email}"):
        session = await manager.owui.signin(email, data["password"])
        manager.validate_session(session)
        if base_url != manager.config.cfg.owui.base_url or not manager.config.user_login:
            raise ValueError("登录配置已变化，请重新登录")
        token = request.app[AUTH].issue_session(session, base_url)
    return session_response(request, token)


async def auth_logout(request):
    principal = request[PRINCIPAL]
    request.app[AUTH].revoke(request.cookies.get(COOKIE, ""))
    for job in list(request.app[MANAGER].jobs.values()):
        if job.principal is principal:
            await request.app[MANAGER].cancel_pairing(job.id)
    response = web.json_response({"authenticated": False})
    response.del_cookie(COOKIE)
    return response


async def auth_change(request):
    data = await payload(request)
    async with request.app[AUTH].credential_check(request.app[PROXY].peer(request), "admin"):
        token = await request.app[AUTH].change_password(data.get("current"), data.get("password"))
    return session_response(request, token)


async def status(request):
    manager = request.app[MANAGER]
    principal = request[PRINCIPAL]
    accounts = manager.accounts(principal)
    return web.json_response({
        "version": __version__, "enabled": manager.enabled, "running": manager.running,
        "error": manager.last_error if principal.role == "admin" else None, "accounts": len(accounts),
        "connected_accounts": sum(item["connected"] for item in accounts),
        "bound_accounts": sum(item["bound"] for item in accounts),
        "owui": await manager.health(force=request.query.get("refresh") == "1"),
    })


async def settings_get(request):
    return web.json_response(request.app[CONFIG].public())


async def settings_save(request):
    data = await payload(request)
    if any(not isinstance(data.get(section, {}), dict)
           for section in ("owui", "display", "capabilities", "reply", "access", "media")):
        raise ValueError("设置分组格式无效")
    manager = request.app[MANAGER]
    async with manager.access_lock:
        previous_url = manager.config.cfg.owui.base_url
        await manager.apply_settings(data)
        if not manager.config.user_login or previous_url != manager.config.cfg.owui.base_url:
            # Do not let sessions revive if the administrator later restores the old settings.
            for token, principal in list(request.app[AUTH].sessions.items()):
                if principal.role == "user":
                    request.app[AUTH].revoke(token)
    for job in list(manager.jobs.values()):
        if job.principal and job.principal.role == "user" and not manager.user_session_active(job.principal):
            await manager.cancel_pairing(job.id)
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
    return web.json_response({"accounts": request.app[MANAGER].accounts(request[PRINCIPAL])})


async def account_action(request):
    manager = request.app[MANAGER]
    data = await payload(request)
    async with manager.access_lock:
        require_account(request)
        return await perform_account_action(request, data)


def require_account(request):
    principal = request[PRINCIPAL]
    if principal.role != "admin" and not request.app[MANAGER].user_session_active(principal):
        raise web.HTTPUnauthorized(text="请重新登录")
    if not request.app[MANAGER].allowed_account(request.match_info["account_id"], request[PRINCIPAL]):
        raise web.HTTPNotFound(text="微信连接不存在")


async def perform_account_action(request, data):
    manager = request.app[MANAGER]
    account_id = request.match_info["account_id"]
    action = request.match_info["action"]
    if action == "bind":
        if request[PRINCIPAL].role == "admin":
            email = login_email(data)
            async with request.app[AUTH].credential_check(
                request.app[PROXY].peer(request), f"owui:{email}"
            ):
                await manager.bind(account_id, email, data["password"])
        else:
            if data:
                raise web.HTTPForbidden(text="只能绑定当前登录的 Open WebUI 账号")
            await manager.bind_session(account_id, request[PRINCIPAL].owui)
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
    async with request.app[MANAGER].access_lock:
        require_account(request)
        return web.json_response({
            "models": await request.app[MANAGER].models(request.match_info["account_id"])
        })


async def pairing_add(request):
    job = await request.app[MANAGER].add_pairing(request[PRINCIPAL])
    return web.json_response(job.public(), status=201)


def pairing(request):
    job = request.app[MANAGER].jobs.get(request.match_info["job_id"])
    if not job:
        raise web.HTTPNotFound(text="扫码任务不存在")
    principal = request[PRINCIPAL]
    if principal.role != "admin" and (
        not job.principal or not job.principal.owui
        or job.principal.base_url != principal.base_url
        or job.principal.owui.user_id != principal.owui.user_id
    ):
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
    app[PROXY] = ProxyPolicy()
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
        application[AUTH].limiter.close()
        logging.getLogger().removeHandler(application[LOGS])
        if LOCK in application:
            application[LOCK].close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.get("/api/auth/state", auth_state), web.post("/api/auth/setup", auth_setup),
        web.post("/api/auth/login", auth_login), web.post("/api/auth/logout", auth_logout),
        web.post("/api/auth/owui", auth_owui),
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
