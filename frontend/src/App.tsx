import { Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import { RequireAdmin, RequireAuth } from './auth/RequireAuth'
import { AdminPage } from './pages/AdminPage'
import { ChatPage } from './pages/ChatPage'
import { DocumentPage } from './pages/DocumentPage'
import { LoginPage } from './pages/LoginPage'
import { ResetPasswordPage } from './pages/ResetPasswordPage'
import { SearchPage } from './pages/SearchPage'
import { SignupPage } from './pages/SignupPage'

export function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/" element={<Navigate to="/search" replace />} />

        {/* Reachable without a session, because they are how one is obtained. */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        {/* Somebody who cannot log in is exactly who needs this one. */}
        <Route path="/reset-password" element={<ResetPasswordPage />} />

        {/* Everything below needs a session. The guard only decides what to
            render -- the backend rejects unauthenticated requests on its own,
            and would still do so if this wrapper were removed. */}
        <Route path="/search" element={<RequireAuth><SearchPage /></RequireAuth>} />
        <Route
          path="/documents/:documentId"
          element={<RequireAuth><DocumentPage /></RequireAuth>}
        />
        {/* One component for both: the session id is simply absent on /chat,
            which is the "nothing selected yet" state. */}
        <Route path="/chat" element={<RequireAuth><ChatPage /></RequireAuth>} />
        <Route path="/chat/:sessionId" element={<RequireAuth><ChatPage /></RequireAuth>} />

        <Route path="/admin" element={<RequireAdmin><AdminPage /></RequireAdmin>} />

        <Route path="*" element={<Navigate to="/search" replace />} />
      </Routes>
    </AuthProvider>
  )
}
