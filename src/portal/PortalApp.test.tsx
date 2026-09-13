import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { PortalApp } from "./PortalApp";
import type { PortalKey, PortalMe, PortalStatus } from "./types";

type Reply = { status?: number; body?: unknown };
type Handler = (body: unknown) => Reply;

// Records every request so a test can prove the portal never asks for operator state.
function stubFetch(routes: Record<string, Handler>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const key = `${init?.method || "GET"} ${input}`;
      calls.push(key);
      const handler = routes[key];
      if (!handler) throw new Error(`Unexpected request ${key}`);
      const { status = 200, body = {} } = handler(
        init?.body ? JSON.parse(String(init.body)) : undefined,
      );
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

const enabled: PortalStatus = {
  enabled: true,
  device_registration_enabled: false,
  signed_in: true,
};

function fixtureMe(overrides: Partial<PortalMe> = {}): PortalMe {
  return {
    account: {
      id: "acct1",
      username: "alice",
      name: "Alice",
      status: "active",
      created: 1_700_000_000,
      activated_at: 1_700_000_100,
      last_login: 1_700_000_200,
    },
    level: {
      id: "lvl1",
      name: "Standard",
      description: "Everyday access",
      routes: [
        { name: "chat", purpose: "General chat", ready: true },
        { name: "private", purpose: "Local only", ready: false },
      ],
      model_patterns: [],
      allow_cloud: false,
      allow_direct_models: true,
      token_budget: { max_tokens: 200_000, window_seconds: 3600 },
      max_concurrency: 2,
    },
    usage: {
      window_start: 1_700_000_000,
      window_seconds: 3600,
      max_tokens: 200_000,
      used: 42_100,
      reserved: 0,
      active_requests: 1,
      max_concurrency: 2,
      resets_at: 1_700_003_600,
    },
    keys: [],
    devices: { observed: [], registered: null },
    source_address: "192.0.2.40",
    base_url: "http://router.test/v1",
    ...overrides,
  };
}

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.useRealTimers();
});

it("shows the not-enabled state without a sign-in form and never requests operator state", async () => {
  const calls = stubFetch({
    "GET /api/v1/portal/status": () => ({
      body: {
        enabled: false,
        device_registration_enabled: false,
        signed_in: false,
      },
    }),
  });
  render(<PortalApp />);
  expect(
    await screen.findByText(
      "User accounts are not enabled on this router. Ask the administrator.",
    ),
  ).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  expect(calls.length).toBeGreaterThan(0);
  expect(
    calls.every((c) => c.split(" ")[1].startsWith("/api/v1/portal/")),
  ).toBe(true);
});

it("activation form appears from the hash token, submits it and clears the hash", async () => {
  history.replaceState(null, "", "/portal/activate#token=mra_secret");
  let signedIn = false;
  const activate = vi.fn(() => {
    signedIn = true;
    return { body: { ok: true } };
  });
  stubFetch({
    "GET /api/v1/portal/status": () => ({
      body: { ...enabled, signed_in: signedIn },
    }),
    "POST /api/v1/portal/activate": activate,
    "GET /api/v1/portal/me": () => ({ body: fixtureMe() }),
  });
  render(<PortalApp />);
  const user = userEvent.setup();
  await user.type(
    await screen.findByLabelText(/^New password/),
    "correct horse battery",
  );
  await user.type(
    screen.getByLabelText("Confirm password"),
    "correct horse battery",
  );
  expect(screen.queryByLabelText("Username")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Activate account" }));
  expect(
    await screen.findByRole("heading", { name: "Alice" }),
  ).toBeInTheDocument();
  expect(activate).toHaveBeenCalledWith({
    token: "mra_secret",
    password: "correct horse battery",
  });
  expect(location.hash).toBe("");
  expect(location.pathname).toBe("/portal");
});

it("overview lists allowed routes with ready dots, the usage meter and limits", async () => {
  stubFetch({
    "GET /api/v1/portal/status": () => ({ body: enabled }),
    "GET /api/v1/portal/me": () => ({ body: fixtureMe() }),
  });
  const { container } = render(<PortalApp />);
  expect(
    await screen.findByRole("heading", { name: "Alice" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Standard · Everyday access")).toBeInTheDocument();
  expect(screen.getByText("chat")).toBeInTheDocument();
  expect(screen.getByText("private")).toBeInTheDocument();
  expect(
    container.querySelectorAll(".portal-routes .status-dot.available"),
  ).toHaveLength(1);
  expect(
    container.querySelectorAll(".portal-routes .status-dot.unavailable"),
  ).toHaveLength(1);
  expect(
    screen.getByText(/42,100 of 200,000 tokens · resets/),
  ).toBeInTheDocument();
  expect(screen.getByRole("progressbar")).toHaveAttribute(
    "aria-valuenow",
    "42100",
  );
  expect(screen.getByText("1 of 2 running")).toBeInTheDocument();
  expect(screen.getByText("http://router.test/v1")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Create your first API key" }),
  ).toBeInTheDocument();
});

it("create key reveals the secret once with the snippet and revoke removes it", async () => {
  let keys: PortalKey[] = [];
  const created = vi.fn((body: unknown) => {
    const record = {
      id: "k1",
      name: (body as { name: string }).name,
      created: 1,
      last_used: null,
    };
    keys = [record];
    return { status: 201, body: { key: "mru_onlyonce", record } };
  });
  const revoked = vi.fn(() => {
    keys = [];
    return { body: { ok: true } };
  });
  stubFetch({
    "GET /api/v1/portal/status": () => ({ body: enabled }),
    "GET /api/v1/portal/me": () => ({ body: fixtureMe({ keys }) }),
    "POST /api/v1/portal/keys": created,
    "DELETE /api/v1/portal/keys/k1": revoked,
  });
  render(<PortalApp />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "API keys" }));
  await user.type(screen.getByLabelText("Key name"), "laptop");
  await user.click(screen.getByRole("button", { name: "Create key" }));
  expect(await screen.findByText("mru_onlyonce")).toBeInTheDocument();
  expect(created).toHaveBeenCalledWith({ name: "laptop" });
  expect(
    screen.getByText(/OPENAI_BASE_URL=http:\/\/router.test\/v1/),
  ).toHaveTextContent("OPENAI_API_KEY=mru_onlyonce");
  const list = screen.getByRole("list", { name: "Your API keys" });
  expect(within(list).getByText("laptop")).toBeInTheDocument();
  await user.click(within(list).getByRole("button", { name: "Revoke" }));
  expect(within(list).getByText("Revoke laptop?")).toBeInTheDocument();
  await user.click(within(list).getByRole("button", { name: "Revoke" }));
  expect(revoked).toHaveBeenCalled();
  expect(
    await screen.findByText(
      "No keys yet. Create one to start sending requests.",
    ),
  ).toBeInTheDocument();
  expect(within(list).queryByText("laptop")).toBeNull();
  expect(screen.queryByText("mru_onlyonce")).toBeNull();
});

it("devices page hides registration when registered is null and shows dev-mode copy, prefilled address and the precedence badge when enabled", async () => {
  let me = fixtureMe({
    devices: {
      observed: [
        {
          source_address: "192.0.2.41",
          software: "python-requests/2.33.0",
          reported_name: "",
          last_seen: 1_700_000_300,
          last_path: "/v1/chat/completions",
          via: "key",
          credential_name: "laptop",
        },
      ],
      registered: null,
    },
  });
  stubFetch({
    "GET /api/v1/portal/status": () => ({ body: enabled }),
    "GET /api/v1/portal/me": () => ({ body: me }),
  });
  const user = userEvent.setup();
  const first = render(<PortalApp />);
  await user.click(await screen.findByRole("button", { name: "Devices" }));
  expect(screen.getByText("Seen recently")).toBeInTheDocument();
  expect(screen.getByText(/192\.0\.2\.41 · key laptop/)).toBeInTheDocument();
  expect(screen.queryByText("Registered devices")).toBeNull();
  expect(screen.queryByText(/Dev mode:/)).toBeNull();
  first.unmount();

  me = fixtureMe({
    devices: {
      observed: [],
      registered: [
        {
          id: "d1",
          address: "192.0.2.40",
          name: "workstation",
          enabled: true,
          created: 1,
          last_matched: null,
          shadowed: true,
        },
      ],
    },
  });
  render(<PortalApp />);
  await user.click(await screen.findByRole("button", { name: "Devices" }));
  expect(screen.getByText("Registered devices")).toBeInTheDocument();
  expect(
    screen.getByText(
      "Dev mode: a registered address can use your allowed routes without a key. Anyone at that address gets your access and counts against your limits.",
    ),
  ).toBeInTheDocument();
  expect(screen.getByLabelText(/^Address/)).toHaveValue("192.0.2.40");
  expect(
    screen.getByText("Takes precedence over an administrator network policy"),
  ).toBeInTheDocument();
});

it("a 403 from /me reloads status and shows the disabled state", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let accountsEnabled = true;
  const status = vi.fn(() => ({
    body: {
      enabled: accountsEnabled,
      device_registration_enabled: false,
      signed_in: accountsEnabled,
    },
  }));
  stubFetch({
    "GET /api/v1/portal/status": status,
    "GET /api/v1/portal/me": () =>
      accountsEnabled
        ? { body: fixtureMe() }
        : {
            status: 403,
            body: { detail: "User accounts are not enabled on this router" },
          },
  });
  render(<PortalApp />);
  expect(
    await screen.findByRole("heading", { name: "Alice" }),
  ).toBeInTheDocument();
  expect(status).toHaveBeenCalledTimes(1);
  accountsEnabled = false;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(
    await screen.findByText(
      "User accounts are not enabled on this router. Ask the administrator.",
    ),
  ).toBeInTheDocument();
  expect(status).toHaveBeenCalledTimes(2);
});
