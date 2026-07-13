import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import LoginPage from "./auth/LoginPage";
import SignupPage from "./auth/SignupPage";
import AgentCataloguePage from "./agents/AgentCataloguePage";
import AgentRunView from "./agents/AgentRunView";
import RunHistoryPage from "./agents/RunHistoryPage";
import AdminPage from "./admin/AdminPage";
import DocumentsPage from "./documents/DocumentsPage";
import RunStatusPage from "./runs/RunStatusPage";
import ComparePage from "./runs/ComparePage";
import ReviewQueuePage from "./review/ReviewQueuePage";
import ReviewDetailPage from "./review/ReviewDetailPage";
import TagVocabularyPage from "./tags/TagVocabularyPage";
import { BotIcon, CheckShieldIcon, FileIcon, GearIcon, LogoutIcon, PlayIcon, TagIcon } from "./components/Icons";
import Brand from "./components/Brand";

function RequireAuth({ children }) {
  const { user, isBootstrapping } = useAuth();
  if (isBootstrapping)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading…
      </div>
    );
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

function Sidebar() {
  const { user, logout } = useAuth();
  if (!user) return null;

  const canSeeAdmin = user.role === "admin" || user.role === "km_governance";
  const canReview = user.role === "admin" || user.role === "km_governance" || user.role === "km_reviewer";
  const roleLabel = user.role.replace(/_/g, " ");

  return (
    <aside className="sidebar">
      <Brand />
      <nav className="side-nav">
        <NavLink to="/agents">
          <BotIcon /> Agents
        </NavLink>
        <NavLink to="/documents">
          <FileIcon /> Documents
        </NavLink>
        <NavLink to="/runs">
          <PlayIcon /> Runs
        </NavLink>
        {canReview && (
          <NavLink to="/review">
            <CheckShieldIcon /> Review
          </NavLink>
        )}
        <NavLink to="/tags">
          <TagIcon /> Tags
        </NavLink>
        {canSeeAdmin && (
          <NavLink to="/admin">
            <GearIcon /> Admin
          </NavLink>
        )}
      </nav>
      <div className="side-user">
        <div className="side-user__avatar">{user.email[0]}</div>
        <div className="side-user__meta">
          <div className="side-user__email" title={user.email}>{user.email}</div>
          <div className="side-user__role">{roleLabel}</div>
        </div>
        <button className="side-user__logout" onClick={logout} title="Log out">
          <LogoutIcon />
        </button>
      </div>
    </aside>
  );
}

function Shell() {
  const { user } = useAuth();
  return (
    <div className="app-shell">
      <Sidebar />
      <main className={user ? "app-main" : "app-main app-main--auth"}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/agents" element={<RequireAuth><AgentCataloguePage /></RequireAuth>} />
          <Route path="/agents/:agentId/run" element={<RequireAuth><AgentRunView /></RequireAuth>} />
          <Route path="/documents" element={<RequireAuth><DocumentsPage /></RequireAuth>} />
          <Route path="/runs" element={<RequireAuth><RunHistoryPage /></RequireAuth>} />
          <Route path="/runs/:runId" element={<RequireAuth><RunStatusPage /></RequireAuth>} />
          <Route path="/runs/:runId/compare" element={<RequireAuth><ComparePage /></RequireAuth>} />
          <Route path="/review" element={<RequireAuth><ReviewQueuePage /></RequireAuth>} />
          <Route path="/review/:runId" element={<RequireAuth><ReviewDetailPage /></RequireAuth>} />
          <Route path="/tags" element={<RequireAuth><TagVocabularyPage /></RequireAuth>} />
          <Route path="/admin" element={<RequireAuth><AdminPage /></RequireAuth>} />
          <Route path="*" element={<Navigate to="/agents" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
