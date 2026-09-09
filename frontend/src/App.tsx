import { Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { DocumentPage } from './pages/DocumentPage'
import { SearchPage } from './pages/SearchPage'

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/search" replace />} />
      <Route path="/search" element={<SearchPage />} />
      <Route path="/documents/:documentId" element={<DocumentPage />} />
      {/* One component for both: the session id is simply absent on /chat,
          which is the "nothing selected yet" state. */}
      <Route path="/chat" element={<ChatPage />} />
      <Route path="/chat/:sessionId" element={<ChatPage />} />
      <Route path="*" element={<Navigate to="/search" replace />} />
    </Routes>
  )
}
