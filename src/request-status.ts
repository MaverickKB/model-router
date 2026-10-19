export function isRequestActive(status: string): boolean {
  return ["running", "routing", "waiting", "recovering"].includes(status);
}
