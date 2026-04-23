/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { Mic, Check, X, Send, ClipboardList, Loader2, AlertCircle, ChevronLeft } from 'lucide-react';
import { apiService } from './services/api';
import { GlowingButton } from './components/ui/glowing-button';

type Screen = 'HOME' | 'RECORDING' | 'REVIEW' | 'COMPLETING';

export default function App() {
  const [screen, setScreen] = useState<Screen>('HOME');
  const [homeView, setHomeView] = useState<'MAIN' | 'LOG_SUB' | 'HANDOVER_SUB' | 'KAKAO_SUB'>('MAIN');
  const [mode, setMode] = useState<'LOG' | 'KAKAO' | 'HANDOVER_SEND' | 'HANDOVER_CONFIRM' | null>(null);
  const [recordedText, setRecordedText] = useState('');
  const [progress, setProgress] = useState(0);
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [handoverAccumulator, setHandoverAccumulator] = useState<string[]>(() => {
    try {
      const saved = sessionStorage.getItem('vg_handover_draft');
      return saved ? JSON.parse(saved) : [];
    } catch { return []; }
  });

  const mediaRecorder = useRef<MediaRecorder | null>(null);
  const audioChunks = useRef<Blob[]>([]);
  const lastTapRef = useRef<number>(0);

  const handleDoubleTap = (action: () => void) => {
    return (e: React.MouseEvent | React.TouchEvent) => {
      e.preventDefault();
      const now = Date.now();
      if (now - lastTapRef.current < 300) {
        action();
      }
      lastTapRef.current = now;
    };
  };

  // Recording timer
  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (screen === 'RECORDING') {
      setSeconds(0);
      interval = setInterval(() => {
        setSeconds(s => {
          if (s >= 34) {
            setTimeout(() => {
              // 35s timeout auto-return to home
              if (mediaRecorder.current && mediaRecorder.current.stream) {
                mediaRecorder.current.stream.getTracks().forEach(track => track.stop());
                try {
                  if (mediaRecorder.current.state !== 'inactive') mediaRecorder.current.stop();
                } catch(e) {}
                mediaRecorder.current = null;
                audioChunks.current = [];
              }

              setScreen('HOME');
              setHomeView('MAIN');
              setMode(null);
            }, 0);
            return 35;
          }
          return s + 1;
        });
      }, 1000);
    }
    return () => clearInterval(interval);
  }, [screen]);

  const formatTime = (s: number) => {
    const mins = Math.floor(s / 60);
    const secs = s % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const textAreaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to bottom when recordedText changes
  useEffect(() => {
    if (textAreaRef.current) {
      textAreaRef.current.scrollTop = textAreaRef.current.scrollHeight;
    }
  }, [recordedText]);

  // Mock completion logic
  useEffect(() => {
    if (screen === 'COMPLETING') {
      let current = 0;
      const interval = setInterval(() => {
        current += 2; // Exact 1.5s loader (100 / 2 = 50 steps * 30ms = 1.5s)
        setProgress(Math.min(current, 100));
        if (current >= 100) {
          clearInterval(interval);
          setTimeout(() => {
            setScreen('HOME');
            setHomeView('MAIN');
            setMode(null);
            setRecordedText('');
            setProgress(0);
          }, 2000);
        }
      }, 30);
      return () => clearInterval(interval);
    }
  }, [screen]);

  const startRecording = async (m: 'LOG' | 'KAKAO') => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      mediaRecorder.current = recorder;
      audioChunks.current = [];

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunks.current.push(event.data);
        }
      };

      // Start recording with 250ms chunks to ensure reliable stream aggregation
      recorder.start(250);
      
      setMode(m);
      setScreen('RECORDING');
      setError(null);
    } catch (err) {
      console.error('Mic error:', err);
      setError('마이크 권한을 허용해주세요.');
    }
  };

  const executeHandoverAction = async (m: 'HANDOVER_SEND' | 'HANDOVER_CONFIRM') => {
    setMode(m);
    setScreen('COMPLETING');
    setProgress(0);
    setError(null);

    try {
      if (m === 'HANDOVER_SEND') {
        if (handoverAccumulator.length === 0) {
          setError('전송할 인수인계 기록이 없습니다. 먼저 기록하기를 눌러주세요.');
          setScreen('HOME');
          return;
        }
        const combinedText = handoverAccumulator.join('\n\n---\n\n');
        await apiService.sendHandover(combinedText);
        setHandoverAccumulator([]);
        sessionStorage.removeItem('vg_handover_draft');
        console.log('[VG-ACCUMULATOR] 2번 버튼 전송 완료 | payload 글자수:', combinedText.length);
      } else {
        await apiService.confirmHandover();
        console.log('[VG-ACCUMULATOR] 3번 버튼 확인 전송 완료');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '전송에 실패했습니다.');
      setScreen('HOME');
    }
  };

  const stopRecording = async () => {
    setIsProcessing(true);
    try {
      let audioBlob: Blob | undefined;
      
      if (mediaRecorder.current && mediaRecorder.current.state !== 'inactive') {
        const stoppedPromise = new Promise<void>((resolve) => {
          mediaRecorder.current!.onstop = () => resolve();
        });
        mediaRecorder.current.stop();
        await stoppedPromise;
        audioBlob = new Blob(audioChunks.current, { type: 'audio/webm' });
      } else if (audioChunks.current && audioChunks.current.length > 0) {
        audioBlob = new Blob(audioChunks.current, { type: 'audio/webm' });
      }

      // Stop all mic tracks to relinquish hardware immediately
      if (mediaRecorder.current?.stream) {
        mediaRecorder.current.stream.getTracks().forEach(track => track.stop());
      }
      
      // Pass the fully assembled Audio Blob to our API Service
      const response = await apiService.transcribeAudio(audioBlob);
      if (response.success) {
        setRecordedText(response.text);
        setScreen('REVIEW');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '음성 인식에 실패했습니다.');
      setScreen('HOME');
      setHomeView('MAIN');
    } finally {
      setIsProcessing(false);
      // Clean up references
      mediaRecorder.current = null;
      audioChunks.current = [];
    }
  };

  const handleExecute = async () => {
    setScreen('COMPLETING');
    try {
      if (mode === 'LOG' && homeView === 'HANDOVER_SUB') {
        // ✅ 1번 버튼(인수인계 기록하기): API 호출 절대 금지. State에 append만.
        const updatedList = [...handoverAccumulator, recordedText.trim()];
        setHandoverAccumulator(updatedList);
        sessionStorage.setItem('vg_handover_draft', JSON.stringify(updatedList));
        console.log(`[VG-ACCUMULATOR] 기록 추가: ${updatedList.length}번째 | 현재 배열 길이: ${updatedList.length}`);
        // 네트워크 호출 없음 — 완료
      } else if (mode === 'LOG') {
        await apiService.saveLog(recordedText); // 기존 업무기록 경로 (무수정)
      } else {
        await apiService.sendKakao(recordedText); // 기존 카카오 경로 (무수정)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '작업 수행에 실패했습니다.');
      setScreen('REVIEW');
    }
  };

  const handleCancel = () => {
    setScreen('HOME');
    setHomeView('MAIN');
    setMode(null);
  };

  return (
    <div className="min-h-screen bg-[#FAFAFA] text-[#111111] font-sans selection:bg-orange-100 flex flex-col items-center justify-center p-4 sm:p-6 overflow-hidden">
      <div className="w-full max-w-md h-[800px] bg-[#FAFAFA] relative flex flex-col shadow-2xl shadow-stone-200/50 rounded-[48px] border border-stone-100/50 overflow-hidden">
        {/* Preload KAKAO logo to prevent rendering delays */}
        <link rel="preload" as="image" href="/KAKAO_LOGO.jpg" />
        <img src="/KAKAO_LOGO.jpg" alt="preload" style={{ display: 'none' }} />
        
        {/* Header */}
        <header className="pt-10 pb-6">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="flex flex-col items-center justify-center w-full"
          >
            <h1 className="text-[32px] sm:text-[36px] font-black uppercase tracking-[0.2em] indent-[0.2em] text-[#1D1B1A] relative z-10 [text-shadow:0_1px_1px_rgba(255,255,255,0.9),0_6px_10px_rgba(0,0,0,0.15),0_2px_4px_rgba(0,0,0,0.08)] text-center">
              VOICE GUARD
            </h1>
          </motion.div>
        </header>

        <main className="flex-1 flex flex-col px-8 pb-12">
          {/* Error Toast */}
          <AnimatePresence>
            {error && (
              <motion.div
                initial={{ opacity: 0, y: -20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -20 }}
                className="absolute top-4 left-4 right-4 bg-red-50 border border-red-200 p-4 rounded-2xl flex items-center gap-3 z-50 shadow-lg"
              >
                <AlertCircle className="w-6 h-6 text-red-500" />
                <p className="text-lg font-bold text-red-700">{error}</p>
                <button onClick={() => setError(null)} className="ml-auto">
                  <X className="w-5 h-5 text-red-400" />
                </button>
              </motion.div>
            )}
          </AnimatePresence>

          <AnimatePresence mode="wait">
            {screen === 'HOME' && homeView === 'MAIN' && (
              <motion.div
                key="home-main"
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                className="flex-1 flex flex-col gap-14 justify-center pt-8"
              >
                <button
                  onClick={() => setHomeView('LOG_SUB')}
                  className="group relative w-full rounded-3xl active:scale-[0.98] transition-all duration-300 flex flex-col items-center justify-center bg-gradient-to-b from-[#FFFFFF] to-[#F9F9F8] shadow-[0_20px_40px_rgba(0,0,0,0.08),_0_4px_12px_rgba(0,0,0,0.04),_inset_0_2px_4px_rgba(255,255,255,1),_inset_0_-4px_10px_rgba(0,0,0,0.03)] border border-stone-100/80 pt-14 pb-10 px-8"
                >
                  {/* Overlapping 3D Icon (Pure Volumetric, Custom 3D SVG) */}
                  <div className="absolute -top-10 left-1/2 -translate-x-1/2 p-4 bg-gradient-to-b from-[#FFFFFF] to-[#F5F5F4] rounded-full shadow-[0_15px_30px_rgba(0,0,0,0.1),_0_4px_10px_rgba(0,0,0,0.05),_inset_0_2px_5px_rgba(255,255,255,1),_inset_0_-3px_8px_rgba(0,0,0,0.05)] border border-stone-200/60 group-hover:-translate-y-1.5 transition-transform duration-300 z-10">
                    <svg fill="none" viewBox="0 0 24 24" className="w-12 h-12 drop-shadow-[0_8px_12px_rgba(255,90,0,0.25)] group-hover:scale-110 group-hover:-rotate-3 transition-transform duration-500">
                      <defs>
                        <linearGradient id="orangeGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                          <stop offset="0%" stopColor="#FF965B"/>
                          <stop offset="100%" stopColor="#E54E00"/>
                        </linearGradient>
                        <linearGradient id="whiteGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                          <stop offset="0%" stopColor="#FFFFFF"/>
                          <stop offset="100%" stopColor="#FCE1D4"/>
                        </linearGradient>
                      </defs>
                      <rect x="4.5" y="4" width="15" height="18" rx="3" fill="url(#orangeGrad)" stroke="#CC4400" strokeWidth="0.5"/>
                      <rect x="6.5" y="8" width="11" height="12" rx="1.5" fill="url(#whiteGrad)"/>
                      <line x1="9" y1="11" x2="15" y2="11" stroke="#FF5A00" strokeWidth="1.5" strokeLinecap="round"/>
                      <line x1="9" y1="14" x2="15" y2="14" stroke="#FF5A00" strokeWidth="1.5" strokeLinecap="round"/>
                      <line x1="9" y1="17" x2="13" y2="17" stroke="#FF5A00" strokeWidth="1.5" strokeLinecap="round"/>
                      <path d="M9 4V2.5C9 1.67157 9.67157 1 10.5 1H13.5C14.3284 1 15 1.67157 15 2.5V4H9Z" fill="url(#whiteGrad)" stroke="#CC4400" strokeWidth="1"/>
                      <circle cx="12" cy="2.5" r="1" fill="#E54E00"/>
                    </svg>
                  </div>
                  {/* Content */}
                  <div className="text-center">
                    <p className="text-3xl font-black text-[#111111]">업무 기록</p>
                    <p className="text-lg font-medium text-stone-500 mt-2">내 업무를 남깁니다</p>
                  </div>
                </button>

                <button
                  onClick={() => setHomeView('KAKAO_SUB')}
                  className="group relative w-full rounded-3xl active:scale-[0.98] transition-all duration-300 flex flex-col items-center justify-center bg-gradient-to-b from-[#FFFFFF] to-[#F9F9F8] shadow-[0_20px_40px_rgba(0,0,0,0.08),_0_4px_12px_rgba(0,0,0,0.04),_inset_0_2px_4px_rgba(255,255,255,1),_inset_0_-4px_10px_rgba(0,0,0,0.03)] border border-stone-100/80 pt-14 pb-10 px-8"
                >
                  {/* Overlapping 3D Kakao Image */}
                  <div className="absolute -top-10 left-1/2 -translate-x-1/2 p-2.5 bg-gradient-to-b from-[#FFFFFF] to-[#F5F5F4] rounded-full shadow-[0_15px_30px_rgba(0,0,0,0.1),_0_4px_10px_rgba(0,0,0,0.05),_inset_0_2px_5px_rgba(255,255,255,1),_inset_0_-3px_8px_rgba(0,0,0,0.05)] border border-stone-200/60 group-hover:-translate-y-1.5 transition-transform duration-300 z-10 flex items-center justify-center">
                    <div className="relative rounded-full shadow-[0_8px_16px_rgba(0,0,0,0.15)] group-hover:scale-105 group-hover:-rotate-3 transition-all duration-500 overflow-hidden w-[56px] h-[56px]">
                      <img 
                        src="/KAKAO_LOGO.jpg" 
                        alt="KakaoTalk Logo" 
                        className="w-full h-full object-cover"
                        referrerPolicy="no-referrer"
                      />
                      {/* Inner 3D Highlight for the image */}
                      <div className="absolute inset-0 rounded-full shadow-[inset_0_3px_8px_rgba(255,255,255,0.5),_inset_0_-3px_8px_rgba(0,0,0,0.1)] pointer-events-none" />
                    </div>
                  </div>
                  {/* Content */}
                  <div className="text-center">
                    <p className="text-3xl font-black text-[#111111]">카카오톡 전송</p>
                    <p className="text-lg font-medium text-stone-500 mt-2">원장님께 바로 보냅니다</p>
                  </div>
                </button>
              </motion.div>
            )}

            {screen === 'HOME' && homeView === 'LOG_SUB' && (
              <motion.div
                key="home-log-sub"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                className="flex-1 flex flex-col gap-14 justify-center relative"
              >
                <div className="relative w-full z-20 mt-4">
                  {/* Floating Back Button precisely above the box (Clear safe margin from the 3D badge) */}
                  <div className="absolute -top-[80px] left-0 z-30">
                    <button onClick={() => setHomeView('MAIN')} className="p-3.5 bg-[#FFFFFF] rounded-full shadow-[0_8px_16px_rgba(0,0,0,0.08),_inset_0_2px_4px_rgba(255,255,255,1)] border border-stone-200/80 active:scale-95 transition-all outline-none flex items-center justify-center">
                      <ChevronLeft className="w-6 h-6 text-stone-700 font-bold drop-shadow-sm" strokeWidth={3} />
                    </button>
                  </div>

                  <button
                    onClick={() => startRecording('LOG')}
                    className="group relative w-full rounded-3xl active:scale-[0.98] transition-all duration-300 flex flex-col items-center justify-center bg-gradient-to-b from-[#FFFFFF] to-[#F9F9F8] shadow-[0_20px_40px_rgba(0,0,0,0.08),_0_4px_12px_rgba(0,0,0,0.04),_inset_0_2px_4px_rgba(255,255,255,1),_inset_0_-4px_10px_rgba(0,0,0,0.03)] border border-stone-100/80 pt-14 pb-10 px-8"
                  >
                    {/* Overlapping 3D Icon Badge */}
                    <div className="absolute -top-10 left-1/2 -translate-x-1/2 p-4 bg-gradient-to-b from-[#FFFFFF] to-[#F5F5F4] rounded-full shadow-[0_15px_30px_rgba(0,0,0,0.1),_0_4px_10px_rgba(0,0,0,0.05),_inset_0_2px_5px_rgba(255,255,255,1),_inset_0_-3px_8px_rgba(0,0,0,0.05)] border border-stone-200/60 group-hover:-translate-y-1.5 transition-transform duration-300 z-10">
                      <svg fill="none" viewBox="0 0 24 24" className="w-12 h-12 drop-shadow-[0_8px_12px_rgba(255,90,0,0.25)] group-hover:scale-110 group-hover:-rotate-3 transition-transform duration-500">
                        <defs>
                          <linearGradient id="orangeGrad2" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stopColor="#FF965B"/>
                            <stop offset="100%" stopColor="#E54E00"/>
                          </linearGradient>
                          <linearGradient id="whiteGrad2" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stopColor="#FFFFFF"/>
                            <stop offset="100%" stopColor="#FCE1D4"/>
                          </linearGradient>
                        </defs>
                        <rect x="4.5" y="4" width="15" height="18" rx="3" fill="url(#orangeGrad2)" stroke="#CC4400" strokeWidth="0.5"/>
                        <rect x="6.5" y="8" width="11" height="12" rx="1.5" fill="url(#whiteGrad2)"/>
                        <line x1="9" y1="11" x2="15" y2="11" stroke="#FF5A00" strokeWidth="1.5" strokeLinecap="round"/>
                        <line x1="9" y1="14" x2="15" y2="14" stroke="#FF5A00" strokeWidth="1.5" strokeLinecap="round"/>
                        <line x1="9" y1="17" x2="13" y2="17" stroke="#FF5A00" strokeWidth="1.5" strokeLinecap="round"/>
                        <path d="M9 4V2.5C9 1.67157 9.67157 1 10.5 1H13.5C14.3284 1 15 1.67157 15 2.5V4H9Z" fill="url(#whiteGrad2)" stroke="#CC4400" strokeWidth="1"/>
                        <circle cx="12" cy="2.5" r="1" fill="#E54E00"/>
                      </svg>
                    </div>
                    <div className="text-center">
                      <p className="text-3xl font-black text-[#111111]">업무기록하기</p>
                    </div>
                  </button>
                </div>

                <button
                  onClick={() => setHomeView('HANDOVER_SUB')}
                  className="group relative w-full rounded-3xl active:scale-[0.98] transition-all duration-300 flex flex-col items-center justify-center bg-gradient-to-b from-[#FFFFFF] to-[#F9F9F8] shadow-[0_20px_40px_rgba(0,0,0,0.08),_0_4px_12px_rgba(0,0,0,0.04),_inset_0_2px_4px_rgba(255,255,255,1),_inset_0_-4px_10px_rgba(0,0,0,0.03)] border border-stone-100/80 pt-14 pb-10 px-8"
                >
                  {/* Overlapping 3D Icon Badge */}
                  <div className="absolute -top-10 left-1/2 -translate-x-1/2 p-4 bg-gradient-to-b from-[#FFFFFF] to-[#F5F5F4] rounded-full shadow-[0_15px_30px_rgba(0,0,0,0.1),_0_4px_10px_rgba(0,0,0,0.05),_inset_0_2px_5px_rgba(255,255,255,1),_inset_0_-3px_8px_rgba(0,0,0,0.05)] border border-stone-200/60 group-hover:-translate-y-1.5 transition-transform duration-300 z-10 flex items-center justify-center">
                    <svg fill="none" viewBox="0 0 24 24" className="w-12 h-12 drop-shadow-[0_12px_16px_rgba(255,90,0,0.25)] group-hover:scale-110 group-hover:-translate-y-1 transition-transform duration-500">
                      {/* Solid 3D Base Person (Previous Worker) */}
                      <circle cx="8" cy="8" r="4" fill="#F9F8F6" stroke="#D4C8BC" strokeWidth="0.5" />
                      <path d="M1.5 21 C1.5 17.5 3.5 14.5 8 14.5 C10.5 14.5 12.5 15.5 13.5 17.5 V21 H1.5 Z" fill="#F9F8F6" stroke="#D4C8BC" strokeWidth="0.5" />
                      
                      {/* Overlapping Solid Orange Person (Next Worker/Handover) - NO GRADIENTS */}
                      <circle cx="16" cy="11" r="4.5" fill="#FF5A00" stroke="#CC4400" strokeWidth="0.5" className="drop-shadow-md" />
                      <path d="M9.5 22 C9.5 17.5 12 15 16 15 C20 15 22.5 17.5 22.5 22 H9.5 Z" fill="#FF5A00" stroke="#CC4400" strokeWidth="0.5" className="drop-shadow-lg" />
                    </svg>
                  </div>
                  <div className="text-center">
                    <p className="text-3xl font-black text-[#111111]">인수인계하기</p>
                  </div>
                </button>
              </motion.div>
            )}

            {screen === 'HOME' && homeView === 'HANDOVER_SUB' && (
              <motion.div
                key="home-handover-sub"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                className="flex-1 flex flex-col gap-4 justify-center"
              >
                <div className="relative flex items-center justify-center mb-6 px-2">
                  <button onClick={() => setHomeView('LOG_SUB')} className="absolute left-2 p-3 bg-[#FFFFFF] rounded-full shadow-sm active:scale-95 transition-all z-10">
                    <ChevronLeft className="w-6 h-6 text-stone-600" />
                  </button>
                  <div className="inline-flex items-center gap-3 bg-white/90 shadow-md rounded-full px-6 py-3">
                    <div className="w-3 h-3 rounded-full bg-[#FF5A00] animate-pulse" />
                    <span className="text-xl font-medium text-[#111111]">
                      인수인계하기
                    </span>
                  </div>
                </div>

                <button
                  onClick={() => startRecording('LOG')}
                  className="group relative w-full p-6 rounded-3xl active:scale-[0.98] transition-all duration-300 flex items-center gap-5 bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
                >
                  <div className="w-14 h-14 rounded-full bg-gradient-to-br from-blue-100 to-blue-200 flex items-center justify-center text-2xl shadow-sm border border-white/50">
                    📝
                  </div>
                  <div className="text-left flex-1">
                    <p className="text-xl font-bold text-[#111111]">인수인계 기록하기</p>
                    <p className="text-sm font-medium text-blue-600 mt-0.5">한 번 터치하여 녹음 시작</p>
                  </div>
                </button>

                <button
                  onClick={handleDoubleTap(() => executeHandoverAction('HANDOVER_SEND'))}
                  className="group relative w-full p-6 rounded-3xl active:scale-[0.98] transition-all duration-300 flex items-center gap-5 bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
                >
                  <div className="w-14 h-14 rounded-full bg-gradient-to-br from-orange-100 to-orange-200 flex items-center justify-center text-2xl shadow-sm border border-white/50">
                    🤝
                  </div>
                  <div className="text-left flex-1">
                    <p className="text-xl font-bold text-[#111111]">인수인계 전송</p>
                    <p className="text-sm font-medium text-orange-600 mt-0.5">두 번 터치하여 전송</p>
                  </div>
                </button>

                <button
                  onClick={handleDoubleTap(() => executeHandoverAction('HANDOVER_CONFIRM'))}
                  className="group relative w-full p-6 rounded-3xl active:scale-[0.98] transition-all duration-300 flex items-center gap-5 bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
                >
                  <div className="w-14 h-14 rounded-full bg-gradient-to-br from-green-100 to-green-200 flex items-center justify-center text-2xl shadow-sm border border-white/50">
                    ✅
                  </div>
                  <div className="text-left flex-1">
                    <p className="text-xl font-bold text-[#111111]">인수인계 확인하기</p>
                    <p className="text-sm font-medium text-green-600 mt-0.5">두 번 터치하여 확인</p>
                  </div>
                </button>
              </motion.div>
            )}

            {screen === 'HOME' && homeView === 'KAKAO_SUB' && (
              <motion.div
                key="home-kakao-sub"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                className="flex-1 flex flex-col gap-4 justify-center"
              >
                <div className="relative flex items-center justify-center mb-6 px-2">
                  <button onClick={() => setHomeView('MAIN')} className="absolute left-2 p-3 bg-[#FFFFFF] rounded-full shadow-sm active:scale-95 transition-all z-10">
                    <ChevronLeft className="w-6 h-6 text-stone-600" />
                  </button>
                  <div className="inline-flex items-center gap-3 bg-white/90 shadow-md rounded-full px-6 py-3">
                    <div className="w-3 h-3 rounded-full bg-[#FF5A00] animate-pulse" />
                    <span className="text-xl font-medium text-[#111111]">
                      카카오톡 전송
                    </span>
                  </div>
                </div>

                <button
                  onClick={() => startRecording('KAKAO')}
                  className="group relative w-full p-6 rounded-3xl active:scale-[0.98] transition-all duration-300 flex items-center gap-5 bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
                >
                  <div className="w-14 h-14 rounded-full bg-gradient-to-br from-red-100 to-red-200 flex items-center justify-center text-2xl shadow-sm border border-white/50">
                    🚨
                  </div>
                  <div className="text-left flex-1">
                    <p className="text-xl font-bold text-[#111111]">원장님 전달하기</p>
                    <p className="text-sm font-bold text-red-600 mt-0.5">긴급상황 - 골든 타임 직통 알림</p>
                  </div>
                </button>

                <button
                  onClick={() => startRecording('KAKAO')}
                  className="group relative w-full p-6 rounded-3xl active:scale-[0.98] transition-all duration-300 flex items-center gap-5 bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
                >
                  <div className="w-14 h-14 rounded-full bg-gradient-to-br from-teal-100 to-teal-200 flex items-center justify-center text-2xl shadow-sm border border-white/50">
                    👥
                  </div>
                  <div className="text-left flex-1">
                    <p className="text-xl font-bold text-[#111111]">관리자와 동료 전달하기</p>
                    <p className="text-sm font-medium text-teal-700 mt-0.5">한 번 터치하여 녹음 시작</p>
                  </div>
                </button>
              </motion.div>
            )}

            {screen === 'RECORDING' && (
              <motion.div
                key="recording"
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -20 }}
                className="flex-1 flex flex-col items-center justify-between py-12"
              >
                <div className="flex flex-col items-center gap-8">
                  <div className="inline-flex items-center gap-3 bg-white/90 shadow-md rounded-full px-6 py-3">
                    <div className="w-3 h-3 rounded-full bg-[#FF5A00] animate-pulse" />
                    <span className="text-xl font-medium text-[#111111]">
                      업무를 기록하고 있어요
                    </span>
                  </div>
                  <p className="text-xl font-medium text-gray-400 opacity-60">
                    말씀하시면 자동으로 적어요
                  </p>
                </div>

                <div className="relative flex items-center justify-center my-8">
                  {/* Large Microphone Button (Restored Outer Plate + Increased Size) */}
                  <motion.button
                    onClick={stopRecording}
                    disabled={isProcessing}
                    animate={{
                      scale: isProcessing ? 1 : [0.96, 1.04, 0.96],
                    }}
                    transition={{
                      duration: 3.0,
                      repeat: Infinity,
                      ease: "easeInOut"
                    }}
                    className={`relative w-52 h-52 rounded-full flex items-center justify-center z-10 transition-all bg-gradient-to-b from-[#FF9F62] to-[#E56319] shadow-[0_15px_35px_rgba(255,127,50,0.4),inset_0_2px_10px_rgba(255,255,255,0.3)] ${
                      isProcessing ? 'cursor-not-allowed opacity-90' : 'active:scale-95'
                    }`}
                  >
                    {/* Clean, Luxurious White Glow BEHIND the inner plate */}
                    <motion.div
                      animate={{
                        scale: isProcessing ? 1 : [1, 1.25, 1],
                        opacity: isProcessing ? 0.6 : [0.8, 1, 0.8],
                      }}
                      transition={{
                        duration: 3.0,
                        repeat: Infinity,
                        ease: "easeInOut"
                      }}
                      className="absolute w-28 h-28 bg-white rounded-full blur-lg shadow-[0_0_50px_rgba(255,255,255,1)] pointer-events-none"
                    />

                    {/* Inner Plate */}
                    <div className="relative w-[120px] h-[120px] rounded-full flex items-center justify-center bg-gradient-to-b from-[#FFAD7A] to-[#D9530F] shadow-[0_10px_20px_rgba(0,0,0,0.2),inset_0_2px_10px_rgba(255,255,255,0.3)] overflow-hidden">
                      {isProcessing ? (
                        <Loader2 className="w-14 h-14 text-white animate-spin drop-shadow-md" />
                      ) : (
                        <Mic className="w-14 h-14 text-white drop-shadow-md" />
                      )}
                    </div>
                  </motion.button>
                </div>

                <div className="w-full flex flex-col items-center gap-10">
                  {/* Larger Waveform */}
                  <div className="flex items-end gap-2 h-20">
                    {[...Array(18)].map((_, i) => (
                      <motion.div
                        key={i}
                        animate={{
                          height: [15, Math.random() * 60 + 15, 15],
                        }}
                        transition={{
                          duration: 0.5 + Math.random() * 0.5,
                          repeat: Infinity,
                          ease: "easeInOut"
                        }}
                        className="w-2.5 bg-orange-300/70 rounded-full"
                      />
                    ))}
                  </div>

                  <div className="text-center space-y-6">
                    <p className="text-2xl font-black text-[#FF5A00] tabular-nums">
                      {formatTime(seconds)}
                    </p>
                    <p className="text-xl font-bold text-gray-400 opacity-40">
                      마이크를 누르면 녹음이 끝납니다
                    </p>
                  </div>
                </div>
              </motion.div>
            )}

            {screen === 'REVIEW' && (
              <motion.div
                key="review"
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -20 }}
                className="flex-1 flex flex-col"
              >
                <div className="flex-1 flex flex-col items-center">
                  <div className="inline-flex items-center gap-3 bg-white/90 shadow-md rounded-full px-6 py-3 mt-8 mb-6">
                    <div className="w-3 h-3 rounded-full bg-[#FF5A00] animate-pulse" />
                    <span className="text-xl font-medium text-[#111111]">
                      내용 확인 및 수정
                    </span>
                  </div>
                  <div className="flex-1 w-full relative bg-[#FFFFFF] rounded-3xl shadow-[0_10px_40px_rgb(0,0,0,0.06)] overflow-hidden">
                    <textarea
                      ref={textAreaRef}
                      value={recordedText}
                      onChange={(e) => setRecordedText(e.target.value)}
                      className="w-full h-full p-10 text-[26px] leading-[1.6] font-medium text-[#111111] focus:outline-none focus:ring-0 border-transparent resize-none bg-transparent overflow-y-auto"
                      autoFocus
                    />
                  </div>
                </div>

                <div className="flex gap-4 h-16 mt-8">
                  <button
                    onClick={handleCancel}
                    className="flex-1 bg-stone-100 text-[#111111] rounded-2xl font-bold text-lg active:scale-95 transition-transform"
                  >
                    취소
                  </button>
                  <button
                    onClick={handleExecute}
                    className="flex-[1.8] bg-[#FF7A00] text-white rounded-2xl font-bold text-lg active:scale-95 transition-transform shadow-[0_8px_20px_rgba(255,122,0,0.3)] border border-[#FF8F22]"
                  >
                    {mode === 'LOG' ? '기록 저장' : '카톡 보내기'}
                  </button>
                </div>
              </motion.div>
            )}

            {screen === 'COMPLETING' && (
              <motion.div
                key="completing"
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -20 }}
                className="flex-1 flex flex-col items-center justify-center gap-12"
              >
                {progress < 100 ? (
                  <div className="w-full flex flex-col items-center gap-12">
                    <div className="w-32 h-32 relative">
                      <Loader2 className="w-full h-full text-[#FF5A00] animate-spin opacity-20" />
                      <div className="absolute inset-0 flex items-center justify-center">
                        <div className="w-6 h-6 bg-[#FF5A00] rounded-full animate-pulse" />
                      </div>
                    </div>
                    <div className="w-full max-w-[300px] space-y-6">
                      <div className="w-full bg-stone-100 h-5 rounded-full overflow-hidden p-1 shadow-inner">
                        <motion.div 
                          className="h-full bg-[#FF5A00] rounded-full"
                          initial={{ width: 0 }}
                          animate={{ width: `${progress}%` }}
                        />
                      </div>
                      <p className="text-2xl font-black text-stone-400 text-center leading-tight whitespace-nowrap">
                        {mode === 'LOG' ? '기록을 안전하게 저장하고 있어요' : 
                         mode === 'KAKAO' ? '메시지를 전송하고 있어요' :
                         mode === 'HANDOVER_SEND' ? '인수인계를 전송하고 있어요' :
                         '인수인계를 확인하고 있어요'}
                      </p>
                    </div>
                  </div>
                ) : (
                  <motion.div
                    initial={{ scale: 0.8, opacity: 0 }}
                    animate={{ scale: 1, opacity: 1 }}
                    className="flex flex-col items-center gap-10"
                  >
                    <div className="w-40 h-40 bg-green-500 rounded-full flex items-center justify-center shadow-2xl shadow-green-100">
                      <Check className="w-20 h-20 text-white" strokeWidth={3} />
                    </div>
                    <div className="text-center space-y-2">
                      <h2 className="text-4xl font-black text-[#111111]">
                        {mode === 'LOG' ? '저장 완료!' : 
                         mode === 'HANDOVER_CONFIRM' ? '확인 완료!' : 
                         '전송 완료!'}
                      </h2>
                      <p className="text-xl font-bold text-stone-400">잠시 후 홈으로 이동합니다</p>
                    </div>
                  </motion.div>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
