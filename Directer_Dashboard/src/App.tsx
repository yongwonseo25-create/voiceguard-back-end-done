/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import MicrophoneApp from './pages/MicrophoneApp';
import DirectorDashboard from './pages/DirectorDashboard';
import AdminDashboard from './pages/AdminDashboard';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* 마이크 앱 — 기본 진입점 (모바일 녹음) */}
        <Route path="/" element={<MicrophoneApp />} />

        {/* 관제탑 대시보드 — /admin 하위 */}
        <Route path="/admin" element={<Layout />}>
          <Route index element={<DirectorDashboard />} />
          <Route path="ops" element={<AdminDashboard />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
