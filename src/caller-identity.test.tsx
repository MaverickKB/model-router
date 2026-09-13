import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import {
  CallerIdentity,
  callerDisplayName,
  callerSummary,
} from "./caller-identity";
import type { ObservedCaller } from "./types";

const caller: ObservedCaller = {
  id: "observed",
  policy_id: null,
  name: "Unidentified caller",
  source_address: "192.0.2.30",
  source_port: 51002,
  software: "python-requests/2.33.0",
  reported_name: "",
  identity_quality: "runtime_hints",
  client_family: "Python requests",
  client_version: "2.33.0",
  client_runtime: "CPython",
  client_os: "macOS",
  client_arch: "arm64",
  identity_hints: {
    stainless_lang: "python",
    stainless_runtime_version: "3.12.5",
  },
  identity_basis: "shared_access",
  first_seen: 100,
  request_count: 3,
  last_seen: 200,
  last_method: "GET",
  last_path: "/v1/models",
  recent_source_ports: [51001, 51002],
};

it("summarizes the direct peer and client library without naming the caller", () => {
  expect(callerSummary(caller)).toBe(
    "192.0.2.30:51002 · Python requests 2.33.0",
  );
});

it("normalizes a legacy generic transport label without hiding its evidence", () => {
  const legacy = {
    ...caller,
    name: "python-requests/2.33.0",
    client_family: undefined,
    client_version: undefined,
    identity_quality: undefined,
  };
  expect(callerDisplayName(legacy)).toBe("192.0.2.30");
  expect(callerSummary(legacy)).toContain("Python requests 2.33.0");
});

it("normalizes every backend-known generic transport in legacy rows", () => {
  for (const name of ["node-fetch/2.6.7", "Go-http-client/1.1"]) {
    const legacy = {
      ...caller,
      name,
      software: name,
      client_family: undefined,
      client_version: undefined,
      identity_quality: undefined,
    };
    expect(callerDisplayName(legacy)).toBe("192.0.2.30");
  }
});

it("leaves browser-like user agents as transport evidence", () => {
  const legacy = {
    ...caller,
    name: "Mozilla/5.0",
    software: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6)",
    client_family: undefined,
    client_version: undefined,
    identity_quality: undefined,
  };
  expect(callerDisplayName(legacy)).toBe("192.0.2.30");
  expect(callerSummary(legacy)).toContain("Mozilla/5.0");
});

it("does not present an unrecognized legacy name/version token as a caller", () => {
  const legacy = {
    ...caller,
    name: "notebook-cli/0.21.0",
    software: "notebook-cli/0.21.0",
    client_family: undefined,
    client_version: undefined,
    identity_quality: undefined,
  };
  expect(callerDisplayName(legacy)).toBe("192.0.2.30");
  expect(callerSummary(legacy)).toContain("notebook-cli/0.21.0");
});

it("shows evidence needed to identify an otherwise generic transport", () => {
  render(<CallerIdentity caller={caller} />);

  expect(screen.getByRole("heading", { name: "192.0.2.30" })).toBeVisible();
  expect(screen.getByText(/No caller key was supplied/)).toBeVisible();
  expect(
    screen.getByText("Client runtime metadata · shared_access"),
  ).toBeVisible();
  expect(screen.getByText("192.0.2.30:51002")).toBeVisible();
  expect(screen.getByText("Python requests 2.33.0")).toBeVisible();
  expect(screen.getByText("CPython")).toBeVisible();
  expect(screen.getByText("macOS")).toBeVisible();
  expect(screen.getByText("arm64")).toBeVisible();
  expect(screen.getByText("3")).toBeVisible();
  expect(screen.getByText("SDK language")).toBeVisible();
  expect(screen.getByText(/library identifies the transport/)).toBeVisible();
});

it("describes unknown credentials as identity evidence rather than a refused request", () => {
  render(
    <CallerIdentity caller={{ ...caller, identity_basis: "unassigned" }} />,
  );

  expect(
    screen.getByText(
      /supplied credentials did not identify a named permission policy/,
    ),
  ).toBeVisible();
  expect(screen.getByText(/Route settings determine access/)).toBeVisible();
  expect(screen.getByText("Last observed identity")).toBeVisible();
  expect(screen.getByText("No named policy identified")).toBeVisible();
  expect(
    screen.queryByText(/No configured permission policy accepted/),
  ).not.toBeInTheDocument();
  expect(
    screen.getByText(
      /Application labels and client metadata are self-reported/,
    ),
  ).toBeVisible();
});

it("uses declared names and identified key policies without inventing an identity", () => {
  expect(callerDisplayName({ ...caller, name: "Stale label" })).toBe(
    "192.0.2.30",
  );
  expect(
    callerDisplayName({ ...caller, reported_name: "Writing assistant" }),
  ).toBe("Writing assistant");
  expect(
    callerDisplayName({
      ...caller,
      name: "Household",
      identity_quality: "policy_key",
    }),
  ).toBe("Household");
  expect(callerDisplayName({ ...caller, source_address: "" })).toBe(
    "Source address unavailable",
  );
});
