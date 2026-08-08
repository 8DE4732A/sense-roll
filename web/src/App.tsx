import { NavLink, Route, Routes } from 'react-router-dom'
import './App.css'
import Overview from './pages/Overview'
import Requests from './pages/Requests'
import ConfigEditor from './pages/ConfigEditor'
import TestPage from './pages/Test'
import InfoPage from './pages/Info'
import LogsPage from './pages/Logs'

function IconChart() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1" y="9" width="3" height="6" rx="0.5"/>
      <rect x="6" y="5" width="3" height="10" rx="0.5"/>
      <rect x="11" y="2" width="3" height="13" rx="0.5"/>
    </svg>
  )
}

function IconList() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <line x1="3" y1="4" x2="13" y2="4"/>
      <line x1="3" y1="8" x2="13" y2="8"/>
      <line x1="3" y1="12" x2="9" y2="12"/>
    </svg>
  )
}

function IconSettings() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="2.5"/>
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41"/>
    </svg>
  )
}

function IconFlask() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M6 2v5L2 13a1 1 0 0 0 .9 1.5h10.2A1 1 0 0 0 14 13L10 7V2"/>
      <line x1="5" y1="2" x2="11" y2="2"/>
    </svg>
  )
}

function IconInfo() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="6.5"/>
      <line x1="8" y1="7" x2="8" y2="11"/>
      <circle cx="8" cy="5" r="0.5" fill="currentColor" stroke="none"/>
    </svg>
  )
}

function IconLog() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2" y="1.5" width="12" height="13" rx="1.5"/>
      <line x1="5" y1="5" x2="11" y2="5"/>
      <line x1="5" y1="8" x2="11" y2="8"/>
      <line x1="5" y1="11" x2="8.5" y2="11"/>
    </svg>
  )
}

export default function App() {
  return (
    <div className="layout">
      <nav className="sidebar">
        <div className="brand">
          <div className="brand-name">sense-roll</div>
          <div className="brand-sub">Admin</div>
        </div>
        <div className="nav-section">
          <div className="nav-label">监控</div>
          <NavLink to="/" end className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            <IconChart />概览
          </NavLink>
          <NavLink to="/requests" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            <IconList />请求明细
          </NavLink>
          <NavLink to="/logs" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            <IconLog />日志
          </NavLink>
          <div className="nav-label" style={{ marginTop: 16 }}>管理</div>
          <NavLink to="/config" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            <IconSettings />配置
          </NavLink>
          <NavLink to="/test" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            <IconFlask />测试
          </NavLink>
          <NavLink to="/info" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            <IconInfo />信息
          </NavLink>
        </div>
        <div className="sidebar-footer">v0.1</div>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/requests" element={<Requests />} />
          <Route path="/config" element={<ConfigEditor />} />
          <Route path="/test" element={<TestPage />} />
          <Route path="/info" element={<InfoPage />} />
          <Route path="/logs" element={<LogsPage />} />
        </Routes>
      </main>
    </div>
  )
}
