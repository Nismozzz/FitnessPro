import { useState } from 'react';
import type { ModelTier } from './api';

const MODEL_KEY = 'fitnesspro.model-tier';

export function useModelTier() {
  const [tier, setTier] = useState<ModelTier>(() => {
    try { return localStorage.getItem(MODEL_KEY) === 'fast' ? 'fast' : 'normal'; }
    catch { return 'normal'; }
  });
  function change(value: ModelTier) {
    setTier(value);
    try { localStorage.setItem(MODEL_KEY, value); } catch { /* 存储不可用时仍允许本次切换。 */ }
  }
  return [tier, change] as const;
}

export function ModelSelector({ value, onChange, disabled }: {
  value: ModelTier; onChange: (value: ModelTier) => void; disabled?: boolean;
}) {
  return <label className="model-selector">
    <span>回答模型</span>
    <select aria-label="回答模型" value={value} disabled={disabled} onChange={event => {
      const next = event.target.value;
      if (next === 'fast' || next === 'normal') onChange(next);
    }}>
      <option value="normal">Normal</option>
      <option value="fast">Fast</option>
    </select>
  </label>;
}
