import { useEffect, useState, type FormEvent } from 'react';
import { ArrowRight, Check, Eye, EyeOff, Settings } from 'lucide-react';
import { request } from './api';

type Endpoint = { model: string; base_url: string; api_key_set: boolean; api_key_hint: string };
type Providers = {
  ready: boolean;
  normal: Endpoint;
  fast: Endpoint;
  embedding: Endpoint & { provider: 'openai' | 'local' };
  reranker: Endpoint & { enabled: boolean };
};
type Draft = { model: string; base_url: string; api_key: string; clear_api_key: boolean; reveal: boolean };
type Props = { token: string | null; login: () => void; onSaved?: (ready: boolean) => void };

const emptyDraft = (endpoint?: Endpoint): Draft => ({ model: endpoint?.model || '', base_url: endpoint?.base_url || '', api_key: '', clear_api_key: false, reveal: false });

export function SettingsPage({ token, login, onSaved }: Props) {
  const [providers, setProviders] = useState<Providers | null>(null);
  const [normal, setNormal] = useState<Draft>(emptyDraft());
  const [fast, setFast] = useState<Draft>(emptyDraft());
  const [embedding, setEmbedding] = useState<Draft>(emptyDraft());
  const [provider, setProvider] = useState<'openai' | 'local'>('openai');
  const [reranker, setReranker] = useState<Draft>(emptyDraft());
  const [rerankEnabled, setRerankEnabled] = useState(true);
  const [shareKey, setShareKey] = useState(true);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]);

  useEffect(() => {
    if (!token) { setProviders(null); return; }
    let active = true;
    setLoading(true); setError('');
    request<Providers>('/settings/providers').then(data => {
      if (!active) return;
      setProviders(data);
      setNormal(emptyDraft(data.normal));
      setFast(emptyDraft(data.fast));
      setEmbedding(emptyDraft(data.embedding));
      setProvider(data.embedding.provider);
      setReranker(emptyDraft(data.reranker));
      setRerankEnabled(data.reranker.enabled);
      setShareKey(!data.fast.api_key_set || data.fast.api_key_hint === data.normal.api_key_hint);
    }).catch(e => { if (active) setError(e instanceof Error ? e.message : '加载失败'); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [token]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setPending(true); setError(''); setSaved(''); setWarnings([]);
    const body = {
      normal: secretPayload(normal),
      fast: secretPayload(fast),
      embedding: { ...secretPayload(embedding), provider },
      reranker: { ...secretPayload(reranker), enabled: rerankEnabled },
      share_normal_key: shareKey,
    };
    try {
      const data = await request<Providers & { warnings?: string[] }>('/settings/providers', { method: 'PUT', body: JSON.stringify(body) });
      setProviders(data);
      setNormal(emptyDraft(data.normal));
      setFast(emptyDraft(data.fast));
      setEmbedding(emptyDraft(data.embedding));
      setReranker(emptyDraft(data.reranker));
      setSaved('连接已保存，下一轮对话会使用这套配置。');
      setWarnings(data.warnings || []);
      onSaved?.(data.ready);
    } catch (err) { setError(err instanceof Error ? err.message : '保存失败'); }
    finally { setPending(false); }
  }

  if (!token) return <section className="training-page"><div className="section-eyebrow">MODEL CONNECTION</div><h1>模型从这里接进来。</h1><div className="training-empty"><Settings size={40}/><h3>登录后填写模型密钥</h3><p>不用再编辑 .env 或 config.yml。密钥只留在这台电脑上。</p><button className="primary-button" onClick={login}>登录 / 注册 <ArrowRight size={16}/></button></div></section>;
  return <section className="training-page settings-page"><div className="section-eyebrow">MODEL CONNECTION</div><h1>模型从这里接进来。</h1><p>填写服务商给出的模型名、接口地址和 API Key。已保存的密钥只显示末四位，留空表示不修改。这里的连接供这台服务上的账号共用，并覆盖配置文件中的同名项。</p>
    {loading && <p className="notice" role="status">正在读取当前连接…</p>}
    {providers && <form className="settings-grid" onSubmit={submit}>
      <EndpointCard title="教练模型" note="对话、制定计划和调整下一周都使用它。" endpoint={providers.normal} draft={normal} setDraft={setNormal} pending={pending} name="教练模型"/>
      <section className="settings-card"><div className="settings-card-head"><h2>辅助模型</h2><p>用于摘要、记忆提取和动作翻译，可以用更快、更便宜的模型。</p></div>
        <label className="share-key"><input type="checkbox" checked={shareKey} disabled={pending} onChange={e => setShareKey(e.target.checked)}/> 与教练模型使用同一把密钥</label>
        <EndpointFields name="辅助模型" endpoint={providers.fast} draft={fast} setDraft={setFast} pending={pending} hideKey={shareKey}/>
      </section>
      <section className="settings-card"><div className="settings-card-head"><h2>动作检索</h2><p>把问题变成向量。更换模型后，已有向量索引会暂停，需要重新入库。</p></div>
        <label className="field-label">提供方式<select aria-label="Embedding 提供方式" value={provider} disabled={pending} onChange={e => setProvider(e.target.value as 'openai' | 'local')}><option value="openai">在线接口</option><option value="local">本机模型</option></select></label>
        <EndpointFields name="Embedding" endpoint={providers.embedding} draft={embedding} setDraft={setEmbedding} pending={pending} hideKey={provider === 'local'}/>
      </section>
      <section className="settings-card"><div className="settings-card-head"><h2>结果重排</h2><p>在检索之后把更相关的动作排到前面。可以关掉。</p></div>
        <label className="share-key"><input type="checkbox" checked={rerankEnabled} disabled={pending} onChange={e => setRerankEnabled(e.target.checked)}/> 启用重排</label>
        <EndpointFields name="重排" endpoint={providers.reranker} draft={reranker} setDraft={setReranker} pending={pending || !rerankEnabled}/>
      </section>
      {error && <div className="error" role="alert">{error}</div>}
      {saved && <p className="save-success" role="status">{saved}</p>}
      {!!warnings.length && <div className="settings-warnings">{warnings.map(item => <p key={item}>{item}</p>)}</div>}
      <button className="primary-button" disabled={pending}>{pending ? '保存中…' : '保存连接'}<Check size={16}/></button>
    </form>}
  </section>;
}

function secretPayload(draft: Draft) {
  return { model: draft.model.trim(), base_url: draft.base_url.trim(), api_key: draft.clear_api_key ? null : draft.api_key.trim() || null, clear_api_key: draft.clear_api_key };
}

function EndpointCard({ title, note, endpoint, draft, setDraft, pending, name }: { title: string; note: string; endpoint: Endpoint; draft: Draft; setDraft: (draft: Draft) => void; pending: boolean; name: string }) {
  return <section className="settings-card"><div className="settings-card-head"><h2>{title}</h2><p>{note}</p></div><EndpointFields name={name} endpoint={endpoint} draft={draft} setDraft={setDraft} pending={pending}/></section>;
}

function EndpointFields({ name, endpoint, draft, setDraft, pending, hideKey = false }: { name: string; endpoint: Endpoint; draft: Draft; setDraft: (draft: Draft) => void; pending: boolean; hideKey?: boolean }) {
  return <div className="settings-fields">
    <label className="field-label">模型名<input aria-label={`${name}名称`} required maxLength={200} value={draft.model} disabled={pending} onChange={e => setDraft({ ...draft, model: e.target.value })}/></label>
    <label className="field-label">接口地址<input aria-label={`${name}接口地址`} required maxLength={300} value={draft.base_url} disabled={pending} onChange={e => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://"/></label>
    {!hideKey && <label className="field-label">API Key<span className="key-status">{draft.clear_api_key ? '保存后清除' : endpoint.api_key_set ? `已保存 ${endpoint.api_key_hint}` : '尚未配置'}</span>
      <span className="key-row"><input aria-label={`${name} API Key`} type={draft.reveal ? 'text' : 'password'} autoComplete="off" maxLength={512} value={draft.api_key} disabled={pending} placeholder={endpoint.api_key_set ? '留空则不修改' : '粘贴 API Key'} onChange={e => setDraft({ ...draft, api_key: e.target.value, clear_api_key: false })}/><button type="button" className="icon-button" aria-label={draft.reveal ? `隐藏${name}密钥` : `显示${name}密钥`} disabled={pending} onClick={() => setDraft({ ...draft, reveal: !draft.reveal })}>{draft.reveal ? <EyeOff size={16}/> : <Eye size={16}/>}</button></span>
      {endpoint.api_key_set && <button type="button" className="text-button key-clear" disabled={pending} onClick={() => setDraft({ ...draft, api_key: '', clear_api_key: !draft.clear_api_key })}>{draft.clear_api_key ? '取消清除' : '清除已保存的密钥'}</button>}
    </label>}
  </div>;
}
