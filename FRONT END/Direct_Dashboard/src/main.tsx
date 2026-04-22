import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import App from './App.tsx';
import VerifyPage from './pages/VerifyPage.tsx';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter basename="/admin">
      <Routes>
        <Route path="/"                    element={<App />} />
        <Route path="/verify/:ledger_id"   element={<VerifyPage />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
