import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from './api';
import type { Memory } from './MemoryPanel';

export function useMemories(token: string | null, completedTurns: number) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [memoryError, setMemoryError] = useState('');
  const generation = useRef(0);
  const currentToken = useRef(token);
  currentToken.current = token;
  const refreshMemories = useCallback(async () => {
    if (!token || currentToken.current !== token) return;
    const version = ++generation.current;
    setRefreshing(true);
    try {
      const rows = await request<Memory[]>('/memories?status=active&status=candidate');
      if (version === generation.current && currentToken.current === token) { setMemories(rows); setMemoryError(''); }
    } catch (e) {
      if (version === generation.current && currentToken.current === token) setMemoryError(e instanceof Error ? e.message : '记忆刷新失败，请重试');
    } finally { if (version === generation.current && currentToken.current === token) setRefreshing(false); }
  }, [token]);
  useEffect(() => {
    setMemories([]); setMemoryError(''); setRefreshing(false);
    void refreshMemories();
    return () => { generation.current++; };
  }, [refreshMemories]);
  useEffect(() => {
    if (!token || !completedTurns) return;
    const timer = window.setTimeout(() => { void refreshMemories(); }, 3000);
    return () => window.clearTimeout(timer);
  }, [token, completedTurns, refreshMemories]);
  return { memories, setMemories, refreshing, memoryError, refreshMemories };
}
