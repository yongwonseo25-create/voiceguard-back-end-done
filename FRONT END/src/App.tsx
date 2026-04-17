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
  const [homeView, setHomeView] = useState<'MAIN' | 'LOG_SUB' | 'KAKAO_SUB'>('MAIN');
  const [mode, setMode] = useState<'LOG' | 'KAKAO' | null>(null);
  const [recordedText, setRecordedText] = useState('');
  const [progress, setProgress] = useState(0);
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);

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
        setSeconds(s => s + 1);
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
        current += 1.5; // Slightly slower for better UX
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

  const startRecording = (m: 'LOG' | 'KAKAO') => {
    setMode(m);
    setScreen('RECORDING');
    setError(null);
  };

  const stopRecording = async () => {
    setIsProcessing(true);
    try {
      // In a real app, you'd pass the actual audio Blob here
      const response = await apiService.transcribeAudio();
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
    }
  };

  const handleExecute = async () => {
    setScreen('COMPLETING');
    try {
      if (mode === 'LOG') {
        await apiService.saveLog(recordedText);
      } else {
        await apiService.sendKakao(recordedText);
      }
      // The progress animation in useEffect will handle the rest
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
        
        {/* Header */}
        <header className="pt-10 pb-6 text-center">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="inline-flex flex-col items-center"
          >
            <h1 className="text-[32px] sm:text-[36px] font-black text-[#111111] uppercase tracking-[0.25em] ml-[0.25em]">
              VOICE GUARD<span className="text-[#FF5A00]">.</span>
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
                  className="group relative w-full rounded-3xl active:scale-[0.98] transition-all duration-300 flex flex-col items-center justify-center bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)] pt-14 pb-10 px-8"
                >
                  {/* Overlapping Icon */}
                  <div className="absolute -top-10 left-1/2 -translate-x-1/2 p-5 bg-[#FFFFFF] rounded-full shadow-[0_12px_30px_rgb(0,0,0,0.08)] group-hover:-translate-y-1 transition-transform duration-300 border border-stone-50">
                    <ClipboardList className="w-10 h-10 text-[#FF5A00]" />
                  </div>
                  {/* Content */}
                  <div className="text-center">
                    <p className="text-3xl font-black text-[#111111]">업무 기록</p>
                    <p className="text-lg font-medium text-stone-500 mt-2">내 업무를 남깁니다</p>
                  </div>
                </button>

                <button
                  onClick={() => setHomeView('KAKAO_SUB')}
                  className="group relative w-full rounded-3xl active:scale-[0.98] transition-all duration-300 flex flex-col items-center justify-center bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)] pt-14 pb-10 px-8"
                >
                  {/* Overlapping Icon */}
                  <div className="absolute -top-10 left-1/2 -translate-x-1/2 p-5 bg-[#FFFFFF] rounded-full shadow-[0_12px_30px_rgb(0,0,0,0.08)] group-hover:-translate-y-1 transition-transform duration-300 border border-stone-50">
                    <Send className="w-10 h-10 text-[#FF5A00]" />
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
                className="flex-1 flex flex-col gap-4 justify-center"
              >
                <div className="relative flex items-center justify-center mb-6 px-2">
                  <button onClick={() => setHomeView('MAIN')} className="absolute left-2 p-3 bg-[#FFFFFF] rounded-full shadow-sm active:scale-95 transition-all z-10">
                    <ChevronLeft className="w-6 h-6 text-stone-600" />
                  </button>
                  <div className="inline-flex items-center gap-3 bg-white/90 shadow-md rounded-full px-6 py-3">
                    <div className="w-3 h-3 rounded-full bg-[#FF5A00] animate-pulse" />
                    <span className="text-xl font-medium text-[#111111]">
                      업무 기록
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
                  onClick={handleDoubleTap(() => startRecording('LOG'))}
                  className="group relative w-full p-6 rounded-3xl active:scale-[0.98] transition-all duration-300 flex items-center gap-5 bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
                >
                  <div className="w-14 h-14 rounded-full bg-gradient-to-br from-orange-100 to-orange-200 flex items-center justify-center text-2xl shadow-sm border border-white/50">
                    🤝
                  </div>
                  <div className="text-left flex-1">
                    <p className="text-xl font-bold text-[#111111]">인수인계하기</p>
                    <p className="text-sm font-medium text-orange-600 mt-0.5">두 번 터치하여 녹음 시작</p>
                  </div>
                </button>

                <button
                  onClick={handleDoubleTap(() => startRecording('LOG'))}
                  className="group relative w-full p-6 rounded-3xl active:scale-[0.98] transition-all duration-300 flex items-center gap-5 bg-[#FFFFFF] shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
                >
                  <div className="w-14 h-14 rounded-full bg-gradient-to-br from-green-100 to-green-200 flex items-center justify-center text-2xl shadow-sm border border-white/50">
                    ✅
                  </div>
                  <div className="text-left flex-1">
                    <p className="text-xl font-bold text-[#111111]">인수인계 확인하기</p>
                    <p className="text-sm font-medium text-orange-600 mt-0.5">두 번 터치하여 녹음 시작</p>
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
                      className="absolute w-36 h-36 bg-white rounded-full blur-lg shadow-[0_0_50px_rgba(255,255,255,1)] pointer-events-none"
                    />

                    {/* Inner Plate */}
                    <div className="relative w-40 h-40 rounded-full flex items-center justify-center bg-gradient-to-b from-[#FFAD7A] to-[#D9530F] shadow-[0_10px_20px_rgba(0,0,0,0.2),inset_0_2px_10px_rgba(255,255,255,0.3)] overflow-hidden">
                      {isProcessing ? (
                        <Loader2 className="w-20 h-20 text-white animate-spin drop-shadow-md" />
                      ) : (
                        <Mic className="w-20 h-20 text-white drop-shadow-md" />
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
                    className="flex-[1.8] bg-[#FF5A00] text-white rounded-2xl font-bold text-lg active:scale-95 transition-transform shadow-[0_8px_20px_rgba(255,90,0,0.3)]"
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
                      <p className="text-2xl font-black text-stone-400 text-center leading-tight">
                        {mode === 'LOG' ? '기록을 안전하게\n저장하고 있어요' : '메시지를\n전송하고 있어요'}
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
                        {mode === 'LOG' ? '저장 완료!' : '전송 완료!'}
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
