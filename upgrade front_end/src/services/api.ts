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
   * Saves the work log to the database.
   */
  async saveLog(text: string): Promise<ActionResponse> {
    try {
      const response = await fetch(`${API_BASE_URL}/api/logs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, timestamp: new Date().toISOString() }),
      });

      if (!response.ok) throw new Error('Failed to save log');
      return { success: true };
    } catch (error) {
      console.error('Save log error:', error);
      // Fallback for demo if API_BASE_URL is not set
      if (!API_BASE_URL) return { success: true };
      throw new Error('기록 저장에 실패했습니다.');
    }
  },

  /**
   * 2번 버튼 전용: 축적된 인수인계 텍스트 일괄 전송 → WORM + Notion 듀얼 라우터
   */
  async sendHandover(combinedText: string): Promise<ActionResponse> {
    try {
      const response = await fetch(`${API_BASE_URL}/api/v8/handover`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: combinedText,
          facility_id: null,
          beneficiary_id: null,
          caregiver_id: null,
        }),
      });
      if (!response.ok) throw new Error('인수인계 전송에 실패했습니다.');
      return { success: true };
    } catch (error) {
      console.error('sendHandover error:', error);
      if (!API_BASE_URL) return { success: true };
      throw new Error('인수인계 전송에 실패했습니다.');
    }
  },

  /**
   * 3번 버튼 전용: 인수자 식별자만 전송 (텍스트 페이로드 없음) → WORM + Notion
   */
  async confirmHandover(): Promise<ActionResponse> {
    try {
      const response = await fetch(`${API_BASE_URL}/api/v8/handover/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          confirmed_at: new Date().toISOString(),
        }),
      });
      if (!response.ok) throw new Error('인수인계 확인 전송에 실패했습니다.');
      return { success: true };
    } catch (error) {
      console.error('confirmHandover error:', error);
      if (!API_BASE_URL) return { success: true };
      throw new Error('인수인계 확인 전송에 실패했습니다.');
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
  }
};
