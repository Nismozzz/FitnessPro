import { useState } from 'react';
import { Activity, BarChart3 } from 'lucide-react';
import { EvaluationPage } from './EvaluationPage';
import { OperationsPage } from './OperationsPage';

type Props = { token: string | null; login: () => void; openSession: (sessionId: string) => void };
export function DashboardPage(props: Props) {
  const [tab, setTab] = useState<'operations' | 'ragas'>('operations');
  return <div className="dashboard-workspace">
    <div className="dashboard-tabs" role="tablist" aria-label="Dashborad 页面">
      <button id="operations-tab" role="tab" aria-selected={tab === 'operations'} aria-controls="operations-panel" onClick={() => setTab('operations')}><Activity size={16}/>运行监控</button>
      <button id="ragas-tab" role="tab" aria-selected={tab === 'ragas'} aria-controls="ragas-panel" onClick={() => setTab('ragas')}><BarChart3 size={16}/>RAGAS 评估</button>
    </div>
    <div className="dashboard-panel" id={`${tab}-panel`} role="tabpanel" aria-labelledby={`${tab}-tab`}>
      {tab === 'operations' ? <OperationsPage {...props}/> : <EvaluationPage {...props}/>}
    </div>
  </div>;
}
