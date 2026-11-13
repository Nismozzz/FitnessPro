import { useState } from 'react';
import { equipmentLabels } from './trainingTypes';
export function TrainingLocationFields({ profile }: { profile: { training_location?: string; equipment?: string[]; excluded_equipment?: string[] } }) {
  const [location, setLocation] = useState(profile.training_location || '');
  return <fieldset className="location-fields"><legend>训练场景与器械</legend>
    <label>通常在哪里训练？<select name="training_location" value={location} onChange={e => setLocation(e.target.value)}><option value="">请选择训练场景</option><option value="gym">商业健身房</option><option value="home">家庭训练</option><option value="bodyweight">徒手训练</option><option value="custom">自定义场地</option></select></label>
    {location === 'gym' && <p className="field-note">默认包含杠铃、哑铃、绳索、固定器械等常见类别，不用逐项填写。缺少某一台机器时，可以在计划页仅替换那个动作。</p>}
    {location !== 'gym' && location !== 'bodyweight' && <><span className="field-label">可用器械（可多选，默认允许徒手）</span><div className="equipment-checks">{Object.entries(equipmentLabels).filter(([v]) => v !== 'body only').map(([value, label]) => <label key={value}><input type="checkbox" name="equipment" value={value} defaultChecked={profile.equipment?.includes(value)}/>{label}</label>)}</div></>}
    <details><summary>有整个类别的器械不可用？</summary><p className="field-note">只有明确缺少整个类别时才勾选。缺少某台固定机器不代表所有固定器械不可用。</p><div className="equipment-checks">{Object.entries(equipmentLabels).map(([value, label]) => <label key={value}><input type="checkbox" name="excluded_equipment" value={value} defaultChecked={profile.excluded_equipment?.includes(value)}/>{label}</label>)}</div></details>
  </fieldset>;
}
