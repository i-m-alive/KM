import { Navigate, Route, Routes, Link } from "react-router-dom";
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

function RequireAuth({ children }) {
  const { user, isBootstrapping } = useAuth();
  if (isBootstrapping) return <p>Loading...</p>;
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

function NavBar() {
  const { user, logout } = useAuth();
  if (!user) return null;

  const canSeeAdmin = user.role === "admin" || user.role === "km_governance";
  const canReview = user.role === "admin" || user.role === "km_governance" || user.role === "km_reviewer";

  return (
    <nav className="nav-bar">
      <Link to="/agents">Agents</Link>
      <Link to="/documents">Documents</Link>
      <Link to="/runs">Runs</Link>
      {canReview && <Link to="/review">Review</Link>}
      <Link to="/tags">Tags</Link>
      {canSeeAdmin && <Link to="/admin">Admin</Link>}
      <span className="nav-bar__spacer" />
      <span>
        {user.email} · {user.role}
      </span>
      <button onClick={logout}>Log out</button>
    </nav>
  );
}

function protectedRoute(element) {
  return <RequireAuth>{element}</RequireAuth>;
}

export default function App() {
  return (
    <AuthProvider>
      <NavBar />
      <main className="app-main">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/agents" element={protectedRoute(<AgentCataloguePage />)} />
          <Route path="/agents/:agentId/run" element={protectedRoute(<AgentRunView />)} />
          <Route path="/documents" element={protectedRoute(<DocumentsPage />)} />
          <Route path="/runs" element={protectedRoute(<RunHistoryPage />)} />
          <Route path="/runs/:runId" element={protectedRoute(<RunStatusPage />)} />
          <Route path="/runs/:runId/compare" element={protectedRoute(<ComparePage />)} />
          <Route path="/review" element={protectedRoute(<ReviewQueuePage />)} />
          <Route path="/review/:runId" element={protectedRoute(<ReviewDetailPage />)} />
          <Route path="/tags" element={protectedRoute(<TagVocabularyPage />)} />
          <Route path="/admin" element={protectedRoute(<AdminPage />)} />
          <Route path="*" element={<Navigate to="/agents" replace />} />
        </Routes>
      </main>
    </AuthProvider>
  );
}
