export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const value = await response
    .json()
    .catch(() => ({ detail: "The router returned an unreadable response" }));
  if (!response.ok) {
    let detail =
      value.detail ||
      value.error?.message ||
      "The request could not be completed";
    if (Array.isArray(detail))
      detail = detail.map((item: { msg: string }) => item.msg).join(". ");
    throw new ApiError(String(detail), response.status);
  }
  return value;
}
export const post = <T>(path: string, body: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
