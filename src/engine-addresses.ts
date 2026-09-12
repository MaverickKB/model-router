import type { Engine } from "./types";

export function engineUrls(engine: Engine): string[] {
  return [...new Set([engine.base_url, ...(engine.aliases || [])])];
}

export function engineHost(engine: Engine): string {
  const addresses = engineUrls(engine).map((url) => new URL(url));
  const named = addresses.find(
    ({ hostname }) =>
      hostname !== "localhost" &&
      !hostname.includes(":") &&
      !/^\d+\.\d+\.\d+\.\d+$/.test(hostname),
  );
  return (named || addresses[0]).host;
}
