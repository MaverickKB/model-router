import type { Client, ObservedCaller } from "./types";

function addressBits(address: string): { value: bigint; width: number } | null {
  if (!address.includes(":")) {
    const octets = address.split(".");
    if (octets.length !== 4) return null;
    let value = 0n;
    for (const octet of octets) {
      if (!/^\d{1,3}$/.test(octet) || Number(octet) > 255) return null;
      value = (value << 8n) | BigInt(octet);
    }
    return { value, width: 32 };
  }
  // URL parsing validates and canonicalizes IPv6, including embedded IPv4.
  let normalized: string;
  try {
    normalized = new URL(`http://[${address}]/`).hostname.slice(1, -1);
  } catch {
    return null;
  }
  const halves = normalized.split("::");
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves[1] ? halves[1].split(":") : [];
  const groups = [
    ...left,
    ...Array(8 - left.length - right.length).fill("0"),
    ...right,
  ];
  let value = 0n;
  for (const group of groups) value = (value << 16n) | BigInt(`0x${group}`);
  return { value, width: 128 };
}

function networkClientName(address: string, clients: Client[]): string {
  const peer = addressBits(address);
  if (!peer) return "";
  let bestPrefix = -1;
  const names = new Set<string>();
  for (const client of clients) {
    for (const cidr of client.source_networks) {
      const parts = cidr.split("/");
      const network = addressBits(parts[0]);
      const prefix = parts.length === 1 ? peer.width : Number(parts[1]);
      if (
        !network ||
        network.width !== peer.width ||
        parts.length > 2 ||
        (parts.length === 2 && !/^\d+$/.test(parts[1])) ||
        !Number.isInteger(prefix) ||
        prefix < 0 ||
        prefix > peer.width
      )
        continue;
      const shift = BigInt(peer.width - prefix);
      if (peer.value >> shift !== network.value >> shift) continue;
      if (prefix > bestPrefix) {
        bestPrefix = prefix;
        names.clear();
      }
      if (prefix === bestPrefix && client.name) names.add(client.name);
    }
  }
  // Conflicting names at the same specificity do not identify one client.
  return names.size === 1 ? [...names][0] : "";
}

export function sourceFriendlyName(
  caller: ObservedCaller,
  clients: Client[] = [],
): string {
  if (
    caller.source_label &&
    caller.source_label_source !== "address" &&
    caller.source_label !== caller.source_address
  )
    return caller.source_label;
  return (
    networkClientName(caller.source_address, clients) ||
    caller.source_hostname ||
    ""
  );
}

export function sourceAgentLabel(callers: ObservedCaller[]): string {
  const names = new Set<string>();
  for (const caller of callers) {
    if (caller.reported_name) names.add(caller.reported_name);
  }
  return [...names].join(", ");
}
