const API_PREFIX = "/api/v1";
let token = "";
const pendingKeys = new Map();

export function setToken(value) {
  token = value;
}

export function hasToken() {
  return Boolean(token);
}

class RequestError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function request(path, {method = "GET", body, key, timeout = 30_000} = {}) {
  const headers = {Accept: "application/json"};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const controller = new AbortController();
  const expiry = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(API_PREFIX + path, {method, headers, credentials: "omit", redirect: "error",
      signal: controller.signal, body: body === undefined ? undefined : JSON.stringify(body)});
    let data;
    try { data = await response.json(); } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new RequestError(`The downloader returned HTTP ${response.status} without a JSON response.`, response.status);
    }
    if (!response.ok) throw new RequestError(data.error || `Request failed (HTTP ${response.status}).`, response.status);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Request timed out. Retry the same action to recover its receipt.");
    throw error;
  } finally {
    clearTimeout(expiry);
  }
}

export async function post(path, body = {}) {
  const signature = path + JSON.stringify(body);
  if (!pendingKeys.has(signature)) {
    const random = crypto.getRandomValues(new Uint8Array(16));
    pendingKeys.set(signature, "console:" + Array.from(random, (value) => value.toString(16).padStart(2, "0")).join(""));
  }
  try {
    const data = await request(path, {method: "POST", body, key: pendingKeys.get(signature)});
    pendingKeys.delete(signature);
    return data;
  } catch (error) {
    if (error.status >= 400 && error.status < 500) pendingKeys.delete(signature);
    throw error;
  }
}
