import { Navigate, Route, Routes, Link } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import LoginPage from "./auth/LoginPage";
import SignupPage from "./auth/SignupPage";
import AgentCataloguePage from "./agents/AgentCataloguePage";
import AgentRunView from "./agents/AgentRunView";
import RunHistoryPage from "./agents/RunHistoryPage";

function RequireAuth({ children }) {
  const { user, isBootstrapping } = useAuth();
  if (isBootstrapping) return <p>Loading...</p>;
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

function NavBar() {
  const { user, logout } = useAuth();
  if (!user) return null;

  return (
    <nav className="nav-bar">
      <Link to="/agents">Agents</Link>
      <Link to="/runs">Run history</Link>
      <span className="nav-bar__spacer" />
      <span>{user.email}</span>
      <button onClick={logout}>Log out</button>
    </nav>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <NavBar />
      <main className="app-main">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route
            path="/agents"
            element={
              <RequireAuth>
                <AgentCataloguePage />
              </RequireAuth>
            }
          />
          <Route
            path="/agents/:agentId/run"
            element={
              <RequireAuth>
                <AgentRunView />
              </RequireAuth>
            }
          />
          <Route
            path="/runs"
            element={
              <RequireAuth>
                <RunHistoryPage />
              </RequireAuth>
            }
          />
          <Route
            path="/runs/:runId"
            element={
              <RequireAuth>
                <AgentRunView />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/agents" replace />} />
        </Routes>
      </main>
    </AuthProvider>
  );
}
