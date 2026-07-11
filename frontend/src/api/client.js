const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

// Access token lives in memory only (never localStorage); the refresh token is an
// httpOnly cookie the browser sends automatically and this code never touches directly.
let accessToken = null;

export const tokenStore = {
  get: () => accessToken,
  set: (token) => {
    accessToken = token;
  },
  clear: () => {
    accessToken = null;
  },
};

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function rawFetch(path, options = {}) {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...options.headers,
    },
  });
  return res;
}

let refreshPromise = null;

async function tryRefresh() {
  if (!refreshPromise) {
    refreshPromise = rawFetch("/auth/refresh", { method: "POST" })
      .then(async (res) => {
        if (!res.ok) {
          tokenStore.clear();
          return null;
        }
        const data = await res.json();
        tokenStore.set(data.access_token);
        return data;
      })
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

export async function apiFetch(path, options = {}) {
  let res = await rawFetch(path, options);

  if (res.status === 401 && path !== "/auth/refresh" && path !== "/auth/login") {
    const refreshed = await tryRefresh();
    if (refreshed) {
      res = await rawFetch(path, options);
    }
  }

  if (!res.ok) {
    let body = null;
    try {
      body = await res.json();
    } catch {
      // no JSON body
    }
    throw new ApiError(body?.detail || `Request to ${path} failed with ${res.status}`, res.status, body);
  }

  if (res.status === 204) return null;
  return res.json();
}

export async function apiPost(path, data) {
  return apiFetch(path, { method: "POST", body: JSON.stringify(data) });
}

export async function apiGet(path) {
  return apiFetch(path, { method: "GET" });
}

export { tryRefresh, ApiError };
