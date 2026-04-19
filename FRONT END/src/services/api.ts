/**
 * API Service for Voice Guard
 * Handles communication with the backend for transcription, logging, and messaging.
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

export interface TranscribeResponse {
  text: string;
  success: boolean;
}

export interface ActionResponse {
  success: boolean;
  message?: string;
}

export const apiService = {
  /**
   * Transcribes audio to text.
   * In a real app, this would send a Blob or base64 audio data.
   */
  async transcribeAudio(audioData?: Blob): Promise<TranscribeResponse> {
    try {
      // Simulate API call delay
      await new Promise(resolve => setTimeout(resolve, 1500));

      // For now, returning mock data as a placeholder for real integration
      // Replace with: const response = await fetch(`${API_BASE_URL}/transcribe`, { method: 'POST', body: audioData });
      return {
        text: "현장 업무 기록입니다. 오늘 오전 10시 자재 입고 완료되었습니다.",
        success: true
      };
    } catch (error) {
      console.error('Transcription error:', error);
      throw new Error('음성 인식에 실패했습니다.');
    }
  },

  /**
   * '업무 기록' 뱃지 전용 — WORM 대시보드 + Notion 듀얼 Fork 라우팅.
   * FE는 단 1번 호출. 백엔드 내부에서 자동 분기:
   *   Fork A → evidence_ledger (WORM, SHA-256 봉인, 수정 불가)
   *   Fork B → care_record_ledger → Notion 업무기록 DB (수정 가능 텍스트 블록)
   */
  async saveLog(text: string): Promise<ActionResponse> {
    try {
      const response = await fetch(`${API_BASE_URL}/api/v8/work-record`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          timestamp: new Date().toISOString(),
          source: 'work_record_badge',
        }),
      });

      if (!response.ok) throw new Error('Failed to save work record');
      return { success: true };
    } catch (error) {
      console.error('Save work record error:', error);
      // Fallback for demo if API_BASE_URL is not set
      if (!API_BASE_URL) return { success: true };
      throw new Error('기록 저장에 실패했습니다.');
    }
  },

  /**
   * Sends the text via KakaoTalk.
   */
  async sendKakao(text: string): Promise<ActionResponse> {
    try {
      const response = await fetch(`${API_BASE_URL}/api/kakao/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });

      if (!response.ok) throw new Error('Failed to send Kakao message');
      return { success: true };
    } catch (error) {
      console.error('Send Kakao error:', error);
      // Fallback for demo if API_BASE_URL is not set
      if (!API_BASE_URL) return { success: true };
      throw new Error('카카오톡 전송에 실패했습니다.');
    }
  },

  /**
   * '인수인계' 전용 — WORM 대시보드 + Notion 듀얼 Fork 라우팅.
   * FE는 단 1번 호출. 백엔드 내부에서 자동 분기:
   *   Fork A → evidence_ledger (WORM, SHA-256 봉인, 수정 불가)
   *   Fork B → care_record_ledger → Notion 인수인계 DB (수정 가능 텍스트 블록)
   */
  async saveHandover(text: string): Promise<ActionResponse> {
    try {
      const response = await fetch(`${API_BASE_URL}/api/v8/handover`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          timestamp: new Date().toISOString(),
          source: 'handover_badge',
          facility_id:    import.meta.env.VITE_FACILITY_ID    || 'F001',
          beneficiary_id: import.meta.env.VITE_BENEFICIARY_ID || 'R001',
          caregiver_id:   import.meta.env.VITE_CAREGIVER_ID   || 'C001',
        }),
      });

      if (!response.ok) throw new Error('Failed to save handover record');
      return { success: true };
    } catch (error) {
      console.error('Save handover error:', error);
      // Fallback for demo if API_BASE_URL is not set
      if (!API_BASE_URL) return { success: true };
      throw new Error('인수인계 저장에 실패했습니다.');
    }
  }
};
