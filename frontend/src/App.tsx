import { Navigate, Route, Routes } from 'react-router-dom'
import { DocumentPage } from './pages/DocumentPage'
import { SearchPage } from './pages/SearchPage'

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/search" replace />} />
      <Route path="/search" element={<SearchPage />} />
      <Route path="/documents/:documentId" element={<DocumentPage />} />
      <Route path="*" element={<Navigate to="/search" replace />} />
    </Routes>
  )
}
