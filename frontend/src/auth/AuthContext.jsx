import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { apiPost, tokenStore, tryRefresh } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [isBootstrapping, setIsBootstrapping] = useState(true);

  useEffect(() => {
    // On page load there's no access token in memory yet, but a refresh
    // cookie may still be valid - try to silently resume the session.
    (async () => {
      const refreshed = await tryRefresh();
      if (refreshed) {
        setUser(refreshed.user);
      }
      setIsBootstrapping(false);
    })();
  }, []);

  const signup = useCallback(async (email, password) => {
    const data = await apiPost("/auth/signup", { email, password });
    tokenStore.set(data.access_token);
    setUser(data.user);
    return data.user;
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await apiPost("/auth/login", { email, password });
    tokenStore.set(data.access_token);
    setUser(data.user);
    return data.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiPost("/auth/logout", {});
    } finally {
      tokenStore.clear();
      setUser(null);
    }
  }, []);

  return (
    <AuthContext.Provider value={{ user, isBootstrapping, signup, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside an AuthProvider");
  return ctx;
}
