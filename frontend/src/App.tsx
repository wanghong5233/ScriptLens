import { Navigate, Route, Routes } from 'react-router-dom'
import DocStudio from './pages/doc-studio'
import Login from './pages/login'
import ReportPage from './pages/report'
import Welcome from './pages/welcome'

export default function App() {
  return (
    <Routes>
      {/* 未登录用户访问 / 应该看到产品介绍而非直接弹登录页（UX 修复）。
          已登录用户进 Welcome 后会在 useEffect 里自动跳 /doc-studio。 */}
      <Route path="/" element={<Welcome />} />
      <Route path="/welcome" element={<Welcome />} />
      <Route path="/login" element={<Login />} />
      <Route path="/doc-studio" element={<DocStudio />} />
      <Route path="/doc-studio/:workspaceId" element={<DocStudio />} />
      <Route path="/scripts/:scriptId/report" element={<ReportPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
