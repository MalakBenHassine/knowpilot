import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { AuthProvider } from './components/AuthProvider'
import { DocumentsProvider } from './components/DocumentsProvider'
import { ProtectedRoute } from './components/ProtectedRoute'
import { AppShell } from './layouts/AppShell'
import { ChatPage } from './pages/ChatPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { LoginPage } from './pages/LoginPage'

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            element={
              <ProtectedRoute>
                <DocumentsProvider>
                  <AppShell />
                </DocumentsProvider>
              </ProtectedRoute>
            }
          >
            <Route path="/documents" element={<DocumentsPage />} />
            <Route path="/chat" element={<ChatPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/documents" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App
