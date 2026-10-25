"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.env.OUI_TEST_URL || "http://127.0.0.1:8790";
const output = path.resolve("reports/browser");

(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  const errors = [];
  const failedAssets = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.url().includes("/static/") && response.status() >= 400) failedAssets.push(response.url());
  });
  const password = crypto.randomBytes(18).toString("hex");
  const nextPassword = crypto.randomBytes(18).toString("hex");
  const checks = [];
  function passed(name) {
    checks.push(name);
    console.log(`PASS ${name}`);
  }

  try {
    await page.goto(base);
    await page.waitForSelector("#auth-screen:not([hidden])");
    assert.equal(
      await page.locator("#auth-title").textContent(),
      "设置管理密码",
      "Run browser tests against a fresh, isolated data directory",
    );
    await page.locator("#auth-password").fill(password);
    await page.locator("#auth-confirm").fill(password);
    await page.locator("#auth-submit").click();
    await page.waitForSelector("#accounts-empty:not([hidden])");
    passed("initial setup and empty account state");
    await page.screenshot({ path: path.join(output, "empty-desktop.png"), fullPage: true });

    await page.locator('[data-view="settings"]').click();
    const reasoning = page.locator('[data-path="display.reasoning.enable"]');
    await reasoning.waitFor();
    assert.equal(await reasoning.isChecked(), true);
    assert.equal(await page.locator('[data-path="display.reasoning.detailed"]').isChecked(), false);
    await page.locator('[data-path="access.user_login"]').check();
    await reasoning.uncheck();
    await page.locator("#settings-form").getByRole("button", { name: "保存设置" }).click();
    await page.waitForFunction(() => !document.getElementById("toast").hidden);
    let response = await page.request.get(`${base}/api/settings`);
    assert.equal((await response.json()).display.reasoning.enable, false);
    await reasoning.check();
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "PUT"),
      page.locator("#settings-form").getByRole("button", { name: "保存设置" }).click(),
    ]);
    response = await page.request.get(`${base}/api/settings`);
    assert.equal((await response.json()).display.reasoning.enable, true);
    passed("reasoning defaults and persisted display settings");
    await page.screenshot({ path: path.join(output, "settings-desktop.png"), fullPage: true });

    await page.locator("#current-password").fill(password);
    await page.locator("#new-password").fill(nextPassword);
    await page.locator("#new-password-confirm").fill(nextPassword);
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/auth/password")),
      page.locator("#password-form").getByRole("button", { name: "更新密码" }).click(),
    ]);
    await page.locator("#logout").click();
    await page.waitForSelector("#auth-screen:not([hidden])");
    await page.locator("#mode-admin").click();
    await page.locator("#auth-password").fill(password);
    await page.locator("#auth-submit").click();
    await page.waitForSelector("#auth-error:not([hidden])");
    await page.locator("#auth-password").fill(nextPassword);
    await page.locator("#auth-submit").click();
    await page.waitForSelector("#app:not([hidden])");
    passed("password change, logout and rejected old password");

    const fixture = [
      {
        id: "demo-personal",
        name: "我的微信",
        bound: true,
        email: "personal@example.com",
        connected: true,
        expired: false,
        model_id: "default",
        chat_title: "日常对话",
        created_at: 1,
      },
      {
        id: "demo-work",
        name: "工作账号",
        bound: true,
        email: "work@example.com",
        connected: true,
        expired: false,
        model_id: "default",
        chat_title: "项目讨论",
        created_at: 2,
      },
      {
        id: "demo-new",
        name: "家人账号",
        bound: false,
        email: null,
        connected: true,
        expired: false,
        model_id: null,
        chat_title: null,
        created_at: 3,
      },
    ];
    let pairingStatus = "wait";
    let activeJob = 0;
    let lastBinding = null;
    const demoPng = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==",
      "base64",
    );
    await page.route("**/api/status*", (route) =>
      route.fulfill({
        json: {
          version: "0.1.0",
          enabled: true,
          running: true,
          accounts: fixture.length,
          connected_accounts: fixture.length,
          bound_accounts: fixture.filter((a) => a.bound).length,
          owui: { connected: true, version: "0.12.0" },
        },
      }),
    );
    await page.route("**/api/accounts", (route) => route.fulfill({ json: { accounts: fixture } }));
    await page.route("**/api/accounts/**", (route) => {
      const url = new URL(route.request().url());
      const parts = url.pathname.split("/");
      const account = fixture.find((item) => item.id === parts[3]);
      const action = parts[4];
      if (action === "models")
        return route.fulfill({
          json: {
            models: [
              { id: "default", name: "默认模型" },
              { id: "light", name: "轻量模型" },
            ],
          },
        });
      const data = route.request().postDataJSON() || {};
      if (action === "bind") {
        lastBinding = data.email;
        account.bound = true;
        account.email = data.email;
        account.model_id = "default";
      } else if (action === "model") account.model_id = data.model_id;
      else if (action === "remove") fixture.splice(fixture.indexOf(account), 1);
      else if (action === "rename") account.name = data.name;
      else if (action === "new-chat") account.chat_title = null;
      return route.fulfill({ json: action === "verify" ? { valid: true } : { ok: true } });
    });
    await page.route("**/api/pairings", (route) => {
      activeJob++;
      pairingStatus = "wait";
      return route.fulfill({ status: 201, json: { id: `job-${activeJob}`, status: "creating" } });
    });
    await page.route("**/api/pairings/**", (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/qr")) return route.fulfill({ body: demoPng, contentType: "image/png" });
      if (url.pathname.endsWith("/code")) {
        assert.equal(route.request().postDataJSON().code, "123456");
        pairingStatus = "confirmed";
        fixture.push({
          id: "demo-paired",
          name: "新微信",
          bound: false,
          email: null,
          connected: true,
          expired: false,
          model_id: null,
          chat_title: null,
          created_at: 4,
        });
        return route.fulfill({ json: { ok: true } });
      }
      if (route.request().method() === "DELETE") return route.fulfill({ json: { ok: true } });
      return route.fulfill({
        json: {
          id: `job-${activeJob}`,
          status: pairingStatus,
          revision: 1,
          image_url: `/api/pairings/job-${activeJob}/qr`,
          account_id: pairingStatus === "confirmed" ? "demo-paired" : null,
        },
      });
    });
    await page.locator('[data-view="accounts"]').click();
    await page.waitForFunction(() => document.querySelectorAll(".account-row").length === 3);
    await page.locator("#toast").waitFor({ state: "hidden" });
    await page.screenshot({ path: path.join(output, "accounts-desktop.png"), fullPage: true });

    await page.locator(".account-row").nth(2).getByRole("button", { name: "绑定账号", exact: true }).click();
    await page.locator("#bind-email").fill("family@example.com");
    await page.locator("#bind-password").fill("fixture-password");
    await page.locator("#bind-form").getByRole("button", { name: "绑定账号", exact: true }).click();
    await page.waitForFunction(() => !document.getElementById("bind-dialog").open);
    assert.equal(lastBinding, "family@example.com");
    assert.equal(await page.locator("#bind-password").inputValue(), "");
    passed("account binding dialog and password clearing");

    await page.locator(".account-row").first().locator("summary").click();
    await page.locator(".account-row").first().getByRole("button", { name: "模型与会话" }).click();
    await page.locator("#model-select").selectOption("light");
    await page.locator("#model-form").getByRole("button", { name: "应用模型" }).click();
    await page.waitForFunction(() => !document.getElementById("model-dialog").open);
    assert.equal(fixture[0].model_id, "light");
    passed("per-account model controls");

    await page.locator("#add-account").click();
    await page.waitForSelector("#qr-image:not([hidden])");
    pairingStatus = "need_verifycode";
    await page.waitForSelector("#pair-code-form:not([hidden])");
    await page.locator("#pair-code").fill("123456");
    await page.locator("#pair-code-form").getByRole("button", { name: "确认配对码" }).click();
    await page.waitForFunction(() => document.getElementById("bind-dialog").open);
    assert.equal(await page.locator("#bind-context").textContent(), "新微信");
    await page.locator('[data-close="bind-dialog"]').first().click();
    passed("QR pairing, verification code and automatic binding step");

    await page.locator(".account-row").nth(2).locator("summary").click();
    await page.locator(".account-row").nth(2).getByRole("button", { name: "移除微信" }).click();
    await page.locator("#confirm-dialog").getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(fixture.length, 4);
    await page.locator(".account-row").nth(2).locator("summary").click();
    await page.locator(".account-row").nth(2).getByRole("button", { name: "移除微信" }).click();
    await page.locator("#confirm-submit").click();
    await page.waitForFunction(() => document.querySelectorAll(".account-row").length === 3);
    assert.deepEqual(
      fixture.map((account) => account.id),
      ["demo-personal", "demo-work", "demo-paired"],
    );
    passed("remove-account confirmation and cancellation preserve the selected scope");

    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await page.waitForTimeout(150);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.equal(await page.locator(".account-row").count(), 3);
      await page.screenshot({ path: path.join(output, `accounts-mobile-${width}.png`), fullPage: true });
      await page.locator('[data-view="settings"]').click();
      await page.waitForSelector('[data-path="display.reasoning.enable"]');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: path.join(output, `settings-mobile-${width}.png`), fullPage: true });
      await page.locator('[data-view="accounts"]').click();
    }
    passed("390px and 320px layouts without horizontal overflow");
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.locator('[data-view="logs"]').click();
    await page.waitForSelector("#view-logs:not([hidden])");
    await page.screenshot({ path: path.join(output, "logs-desktop.png"), fullPage: true });
    const broken = await page.evaluate(() =>
      [...document.images]
        .filter((img) => img.offsetWidth && (!img.complete || img.naturalWidth === 0))
        .map((img) => img.src),
    );
    assert.deepEqual(broken, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(failedAssets, []);
    passed("local assets and no browser JavaScript errors");

    const userContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
    const userPage = await userContext.newPage();
    userPage.on("pageerror", (error) => errors.push(error.message));
    await userPage.goto(base);
    await userPage.waitForSelector("#auth-email-field:not([hidden])");
    await userPage.screenshot({ path: path.join(output, "user-login-desktop.png"), fullPage: true });
    await userPage.locator("#auth-email").fill("alice@example.com");
    await userPage.locator("#auth-password").fill("fixture-password");
    await userPage.locator("#auth-submit").click();
    await userPage.waitForSelector("#accounts-empty:not([hidden])");
    assert.equal(await userPage.locator('[data-view="settings"]').isVisible(), false);
    assert.equal(await userPage.locator('[data-view="logs"]').isVisible(), false);
    assert.equal(await userPage.locator("#service-toggle").isVisible(), false);
    assert.match(await userPage.locator("#signed-in-user").textContent(), /alice@example.com/);
    assert.equal((await userPage.request.get(`${base}/api/logs`)).status(), 403);
    passed("Open WebUI login and user-only empty state with admin APIs forbidden");

    await userPage.locator("#add-account").click();
    await userPage.waitForSelector("#pair-code-form:not([hidden])");
    await userPage.screenshot({ path: path.join(output, "user-pairing-desktop.png"), fullPage: true });
    await userPage.locator("#pair-code").fill("123456");
    await userPage.locator("#pair-code-form").getByRole("button", { name: "确认配对码" }).click();
    await userPage.waitForSelector(".account-row");
    assert.equal(await userPage.locator("#bind-dialog").isVisible(), false);
    assert.match(await userPage.locator(".account-row").textContent(), /alice@example.com/);
    let userData = await (await userPage.request.get(`${base}/api/accounts`)).json();
    const ownedId = userData.accounts[0].id;
    assert.equal(userData.accounts[0].bound, true);
    await userPage.screenshot({ path: path.join(output, "user-accounts-desktop.png"), fullPage: true });
    passed("real pairing API automatically binds the current user without a credential dialog");

    await userPage.locator(".account-row summary").click();
    await userPage.getByRole("button", { name: "解除绑定", exact: true }).click();
    await userPage.locator("#confirm-submit").click();
    await userPage.waitForFunction(() => document.querySelector(".account-row").textContent.includes("未绑定"));
    await userPage.getByRole("button", { name: "绑定账号", exact: true }).first().click();
    await userPage.waitForFunction(() => document.querySelector(".account-row").textContent.includes("alice@example.com"));
    assert.equal(await userPage.locator("#bind-dialog").isVisible(), false);
    passed("self-service unbind and rebind always use the current identity");

    const bobContext = await browser.newContext();
    const bobPage = await bobContext.newPage();
    await bobPage.goto(base);
    await bobPage.waitForSelector("#auth-email-field:not([hidden])");
    await bobPage.locator("#auth-email").fill("bob@example.com");
    await bobPage.locator("#auth-password").fill("fixture-password");
    await bobPage.locator("#auth-submit").click();
    await bobPage.waitForSelector("#accounts-empty:not([hidden])");
    assert.equal((await bobPage.request.get(`${base}/api/accounts/${ownedId}/models`)).status(), 404);
    userData = await (await page.request.get(`${base}/api/accounts`)).json();
    assert.equal(userData.accounts.length, 1);
    assert.equal(userData.accounts[0].email, "alice@example.com");
    passed("second user cannot see the first user's connection while admin can see it");

    for (const width of [390, 320]) {
      await userPage.setViewportSize({ width, height: 844 });
      assert.equal(await userPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await userPage.screenshot({ path: path.join(output, `user-accounts-mobile-${width}.png`), fullPage: true });
    }
    await userPage.locator("#logout").click();
    await userPage.waitForSelector("#auth-screen:not([hidden])");
    await userPage.route("**/api/auth/owui", (route) => route.fulfill({
      status: 429, json: { error: "尝试过于频繁，请 2 秒后重试", retry_after: 2 },
    }));
    await userPage.locator("#auth-email").fill("alice@example.com");
    await userPage.locator("#auth-password").fill("fixture-password");
    await userPage.locator("#auth-submit").click();
    await userPage.waitForSelector("#auth-error:not([hidden])");
    assert.equal(await userPage.locator("#auth-submit").isDisabled(), true);
    await userPage.screenshot({ path: path.join(output, "user-rate-limit-mobile-320.png"), fullPage: true });
    await userPage.waitForFunction(() => !document.getElementById("auth-submit").disabled);
    assert.deepEqual(errors, []);
    passed("mobile user layouts and login retry countdown");
    await bobContext.close();
    await userContext.close();
    await fs.writeFile(
      path.join(output, "summary.json"),
      JSON.stringify(
        {
          checks,
          errors,
          failedAssets,
          scope: "Real local web/auth/ownership APIs with fixture Open WebUI and WeChat; selected admin row flows use browser response fixtures",
        },
        null,
        2,
      ),
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
