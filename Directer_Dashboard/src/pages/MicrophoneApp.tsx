import { useState, useRef, useEffect } from 'react';
import { Mic, MicOff, Shield, CheckCircle, AlertTriangle, Settings, ChevronDown } from 'lucide-react';
import { cn } from '../lib/utils';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

type RecordState = 'idle' | 'recording' | 'submitting' | 'success' | 'error';

const SHIFT_OPTIONS = [
  { value: 'morning', label: '오전 (07:00~14:00)' },
  { value: 'afternoon', label: '오후 (14:00~21:00)' },
  { value: 'night', label: '야간 (21:00~07:00)' },
];

const CARE_TYPES = [
  { value: 'meal', label: '식사' },
  { value: 'medication', label: '투약' },
  { value: 'excretion', label: '배설' },
  { value: 'position_change', label: '체위변경' },
  { value: 'hygiene', label: '위생' },
  { value: 'special', label: '특이사항' },
];

function getStoredField(key: string, fallback: string) {
  return localStorage.getItem(`vg_${key}`) || fallback;
}

export default function MicrophoneApp() {
  const [recordState, setRecordState] = useState<RecordState>('idle');
  const [elapsedSec, setElapsedSec] = useState(0);
  const [ledgerId, setLedgerId] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  const [showSettings, setShowSettings] = useState(false);

  const [facilityId, setFacilityId] = useState(() => getStoredField('facility_id', ''));
  const [userId, setUserId] = useState(() => getStoredField('user_id', ''));
  const [beneficiaryId, setBeneficiaryId] = useState(() => getStoredField('beneficiary_id', ''));
  const [shiftId, setShiftId] = useState(() => getStoredField('shift_id', 'morning'));
  const [careType, setCareType] = useState(() => getStoredField('care_type', 'meal'));

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isConfigComplete = facilityId.trim() && userId.trim() && beneficiaryId.trim();

  useEffect(() => {
    localStorage.setItem('vg_facility_id', facilityId);
    localStorage.setItem('vg_user_id', userId);
    localStorage.setItem('vg_beneficiary_id', beneficiaryId);
    localStorage.setItem('vg_shift_id', shiftId);
    localStorage.setItem('vg_care_type', careType);
  }, [facilityId, userId, beneficiaryId, shiftId, careType]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const startRecording = async () => {
    if (!isConfigComplete) {
      setShowSettings(true);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
      chunksRef.current = [];
      mr.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      mr.onstop = () => stream.getTracks().forEach(t => t.stop());
      mr.start(250);
      mediaRecorderRef.current = mr;
      setRecordState('recording');
      setElapsedSec(0);
      timerRef.current = setInterval(() => setElapsedSec(s => s + 1), 1000);
    } catch {
      setErrorMsg('마이크 권한이 필요합니다. 브라우저 설정에서 허용해 주세요.');
      setRecordState('error');
    }
  };

  const stopAndSubmit = async () => {
    const mr = mediaRecorderRef.current;
    if (!mr || mr.state === 'inactive') return;
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }

    setRecordState('submitting');

    await new Promise<void>(resolve => {
      mr.onstop = () => { mediaRecorderRef.current?.stream?.getTracks().forEach(t => t.stop()); resolve(); };
      mr.stop();
    });

    const blob = new Blob(chunksRef.current, { type: 'audio/webm;codecs=opus' });
    const formData = new FormData();
    formData.append('audio_file', blob, 'recording.webm');
    formData.append('facility_id', facilityId.trim());
    formData.append('beneficiary_id', beneficiaryId.trim());
    formData.append('shift_id', shiftId);
    formData.append('user_id', userId.trim());
    formData.append('care_type', careType);

    try {
      const res = await fetch(`${API_BASE_URL}/api/v2/ingest`, {
        method: 'POST',
        body: formData,
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`서버 오류 ${res.status}: ${txt}`);
      }
      const data = await res.json();
      setLedgerId(data.ledger_id || data.id || '확인중');
      setRecordState('success');
    } catch (e: unknown) {
      setErrorMsg(e instanceof Error ? e.message : '서버 전송 실패');
      setRecordState('error');
    }
  };

  const reset = () => {
    setRecordState('idle');
    setElapsedSec(0);
    setLedgerId('');
    setErrorMsg('');
  };

  const formatTime = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

  return (
    <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center px-4 py-8">
      {/* Header */}
      <div className="w-full max-w-sm mb-8 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Shield className="w-8 h-8 text-blue-500 drop-shadow-[0_0_10px_rgba(59,130,246,0.5)]" />
          <div>
            <h1 className="text-white font-extrabold text-xl tracking-tight">Voice Guard</h1>
            <p className="text-slate-400 text-xs">6대 의무기록 음성 증거 원장</p>
          </div>
        </div>
        <button
          onClick={() => setShowSettings(s => !s)}
          className={cn(
            "p-3 rounded-xl border transition-all",
            showSettings
              ? "bg-blue-600/20 border-blue-500/40 text-blue-400"
              : "bg-slate-800/60 border-slate-700/50 text-slate-400 hover:text-white"
          )}
        >
          <Settings className="w-5 h-5" />
        </button>
      </div>

      {/* Settings Panel */}
      {showSettings && (
        <div className="w-full max-w-sm mb-6 bg-slate-800/60 backdrop-blur-md border border-slate-700/50 rounded-2xl p-5 shadow-xl space-y-4">
          <h2 className="text-white font-bold text-base mb-1">현장 정보 설정</h2>
          <Field label="기관 ID" value={facilityId} onChange={setFacilityId} placeholder="facility-001" />
          <Field label="직원 ID" value={userId} onChange={setUserId} placeholder="user-001" />
          <Field label="수급자 ID" value={beneficiaryId} onChange={setBeneficiaryId} placeholder="beneficiary-001" />
          <SelectField label="교대 근무" value={shiftId} onChange={setShiftId} options={SHIFT_OPTIONS} />
          <SelectField label="기록 유형" value={careType} onChange={setCareType} options={CARE_TYPES} />
          <button
            onClick={() => setShowSettings(false)}
            disabled={!isConfigComplete}
            className="w-full py-3 bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-500 text-white font-bold rounded-xl transition-all"
          >
            {isConfigComplete ? '저장 완료' : '기관 ID · 직원 ID · 수급자 ID 필수'}
          </button>
        </div>
      )}

      {/* Main Recording Panel */}
      <div className="w-full max-w-sm">
        {/* Care Type Pill */}
        {!showSettings && (
          <div className="flex justify-center mb-6">
            <span className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-blue-500/15 border border-blue-500/30 text-blue-300 text-sm font-bold">
              {CARE_TYPES.find(c => c.value === careType)?.label ?? '기록 유형 선택'}
              <ChevronDown className="w-3.5 h-3.5" onClick={() => setShowSettings(true)} />
            </span>
          </div>
        )}

        {/* Record Button Area */}
        {(recordState === 'idle' || recordState === 'recording') && (
          <div className="flex flex-col items-center gap-8">
            {/* Main Button */}
            <button
              onClick={recordState === 'idle' ? startRecording : stopAndSubmit}
              className={cn(
                "w-48 h-48 rounded-full flex items-center justify-center transition-all duration-300 relative",
                recordState === 'recording'
                  ? "bg-red-500/20 border-2 border-red-500 shadow-[0_0_60px_rgba(239,68,68,0.4)]"
                  : isConfigComplete
                    ? "bg-blue-600/20 border-2 border-blue-500 shadow-[0_0_40px_rgba(59,130,246,0.3)] hover:shadow-[0_0_60px_rgba(59,130,246,0.5)] hover:-translate-y-1 active:scale-95"
                    : "bg-slate-800/60 border-2 border-slate-600 text-slate-500"
              )}
            >
              {recordState === 'recording' && (
                <>
                  <span className="absolute inset-0 rounded-full animate-ping bg-red-500/20" />
                  <span className="absolute inset-[-12px] rounded-full animate-pulse bg-red-500/10" />
                </>
              )}
              {recordState === 'recording'
                ? <MicOff className="w-20 h-20 text-red-400 relative z-10" />
                : <Mic className={cn("w-20 h-20 relative z-10", isConfigComplete ? "text-blue-400" : "text-slate-500")} />
              }
            </button>

            {/* Timer / Hint */}
            {recordState === 'recording' ? (
              <div className="text-center">
                <div className="text-red-400 font-mono text-4xl font-black tabular-nums">{formatTime(elapsedSec)}</div>
                <p className="text-slate-400 text-sm mt-2">녹음 중... 탭하면 전송합니다</p>
              </div>
            ) : (
              <div className="text-center">
                <p className="text-slate-300 font-bold text-lg">
                  {isConfigComplete ? '버튼을 눌러 녹음 시작' : '⚙️ 설정을 먼저 완료하세요'}
                </p>
                {isConfigComplete && (
                  <p className="text-slate-500 text-sm mt-1">{SHIFT_OPTIONS.find(s => s.value === shiftId)?.label}</p>
                )}
              </div>
            )}
          </div>
        )}

        {/* Submitting */}
        {recordState === 'submitting' && (
          <div className="flex flex-col items-center gap-6 py-12">
            <div className="w-24 h-24 rounded-full border-4 border-blue-500/30 border-t-blue-500 animate-spin" />
            <div className="text-center">
              <p className="text-white font-bold text-xl">원장 봉인 중...</p>
              <p className="text-slate-400 text-sm mt-1">WORM 해시체인 기록</p>
            </div>
          </div>
        )}

        {/* Success */}
        {recordState === 'success' && (
          <div className="flex flex-col items-center gap-6 py-8">
            <div className="w-28 h-28 rounded-full bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center shadow-[0_0_40px_rgba(16,185,129,0.2)]">
              <CheckCircle className="w-14 h-14 text-emerald-400" />
            </div>
            <div className="text-center space-y-2">
              <h2 className="text-emerald-400 font-extrabold text-2xl">원장 봉인 완료</h2>
              <p className="text-slate-400 text-sm">증거 원장에 불변 기록되었습니다</p>
              {ledgerId && (
                <code className="block mt-3 text-xs text-slate-500 bg-slate-800/80 rounded-lg px-3 py-2 font-mono break-all">
                  {ledgerId}
                </code>
              )}
            </div>
            <button
              onClick={reset}
              className="mt-4 px-8 py-4 bg-blue-600 hover:bg-blue-500 text-white font-bold rounded-2xl transition-all shadow-lg shadow-blue-500/30"
            >
              다음 기록 시작
            </button>
          </div>
        )}

        {/* Error */}
        {recordState === 'error' && (
          <div className="flex flex-col items-center gap-6 py-8">
            <div className="w-28 h-28 rounded-full bg-red-500/10 border border-red-500/30 flex items-center justify-center">
              <AlertTriangle className="w-14 h-14 text-red-400" />
            </div>
            <div className="text-center space-y-2">
              <h2 className="text-red-400 font-extrabold text-2xl">전송 실패</h2>
              <p className="text-slate-400 text-sm max-w-xs break-words">{errorMsg}</p>
            </div>
            <button onClick={reset} className="mt-4 px-8 py-4 bg-slate-700 hover:bg-slate-600 text-white font-bold rounded-2xl transition-all">
              다시 시도
            </button>
          </div>
        )}
      </div>

      {/* Admin Link */}
      <div className="mt-12 text-center">
        <a href="/admin" className="text-slate-600 hover:text-slate-400 text-xs transition-colors">
          관제탑 대시보드 →
        </a>
      </div>
    </div>
  );
}

function Field({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <div className="space-y-1">
      <label className="text-slate-400 text-xs font-bold">{label}</label>
      <input
        type="text"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full px-4 py-3 bg-slate-900/80 border border-slate-700/50 rounded-xl text-white text-sm placeholder-slate-600 focus:outline-none focus:border-blue-500/50 transition-colors"
      />
    </div>
  );
}

function SelectField({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[] }) {
  return (
    <div className="space-y-1">
      <label className="text-slate-400 text-xs font-bold">{label}</label>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-full px-4 py-3 bg-slate-900/80 border border-slate-700/50 rounded-xl text-white text-sm focus:outline-none focus:border-blue-500/50 transition-colors"
      >
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  );
}
