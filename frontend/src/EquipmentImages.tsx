import { useEffect, useRef, useState } from 'react';
import { ImagePlus, X } from 'lucide-react';
import { request, TOKEN_KEY } from './api';

export function splitImageMessage(content: string) {
  const ids = [...content.matchAll(/\[图片附件：([a-f0-9]{32})\]/g)].map(match => match[1]);
  return { text: content.replace(/\n?\[图片附件：[a-f0-9]{32}\]/g, ''), ids };
}

export function EquipmentPhoto({ id, disabled, removed, fail, thumbnail = false }: { id: string; disabled: boolean; thumbnail?: boolean; removed?: () => void; fail: (e: unknown) => void }) {
  const [src, setSrc] = useState('');
  const [missing, setMissing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  useEffect(() => {
    let active = true, objectUrl = '';
    const controller = new AbortController();
    fetch(`/api/v1/images/${id}`, { signal: controller.signal, headers: { Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}` } })
      .then(async response => { if (!response.ok) throw new Error('图片已删除或无法读取'); return response.blob(); })
      .then(blob => { if (active) { objectUrl = URL.createObjectURL(blob); setSrc(objectUrl); } })
      .catch(() => { if (active) setMissing(true); });
    return () => { active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [id]);
  async function remove() {
    if (!window.confirm('删除这张器械照片？删除后将无法再次查看或识别。')) return;
    setDeleting(true);
    try { await request(`/images/${id}`, { method: 'DELETE' }); setMissing(true); removed?.(); }
    catch (e) { fail(e); } finally { setDeleting(false); }
  }
  return <div className={`equipment-photo${thumbnail ? ' equipment-thumbnail' : ''}`}>{missing ? <span>图片已删除或无法读取</span> : <>{src ? <img src={src} alt="上传的健身器械"/> : <span>正在读取图片…</span>}<button type="button" disabled={disabled || deleting} onClick={() => void remove()} aria-label="删除器械照片" title={deleting ? '删除中…' : '删除照片'}><X size={14}/></button></>}</div>;
}

type ImageConfig = { available: boolean; max_upload_bytes: number; provider: string | null; notice: string };
export function EquipmentImagePicker({ token, ids, onChange, disabled, uploading, fail }: { token: string | null; ids: string[]; onChange: (ids: string[]) => void; disabled: boolean; uploading: (value: boolean) => void; fail: (e: unknown) => void }) {
  const [config, setConfig] = useState<ImageConfig | null>(null);
  const [configError, setConfigError] = useState(false);
  const [retry, setRetry] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let active = true; setConfig(null); setConfigError(false);
    if (token) request<ImageConfig>('/images/config').then(value => { if (active) setConfig(value); }).catch(() => { if (active) setConfigError(true); });
    return () => { active = false; };
  }, [token, retry]);
  async function upload(file?: File) {
    if (!file || !config?.available) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) { fail(new Error('仅支持 JPEG、PNG、WebP 图片')); return; }
    if (file.size > config.max_upload_bytes) { fail(new Error(`图片不能超过 ${Math.round(config.max_upload_bytes / 1024 / 1024)} MiB`)); return; }
    uploading(true);
    try { const value = await request<{ id: string }>('/images', { method: 'POST', headers: { 'Content-Type': file.type }, body: file }); onChange([value.id]); }
    catch (e) { fail(e); } finally { uploading(false); }
  }
  const hint = !token ? '登录后可上传' : configError ? '图片配置加载失败，点击重试' : !config ? '正在加载图片配置' : !config.available ? '请先配置并启用视觉模型' : ids.length ? '每次最多上传一张图片' : '上传器械照片';
  return <div className="equipment-upload">
    {ids.length > 0 && <div className="equipment-previews">{ids.map(id => <EquipmentPhoto key={id} id={id} thumbnail disabled={disabled} removed={() => onChange(ids.filter(value => value !== id))} fail={fail}/>)}</div>}
    <div className="equipment-upload-action"><input hidden ref={input} type="file" accept="image/jpeg,image/png,image/webp" aria-label="选择器械照片" onChange={e => { void upload(e.target.files?.[0]); e.target.value = ''; }}/>
      <button type="button" className="equipment-upload-button" aria-label={configError ? '重试加载图片配置' : '上传器械照片'} title={hint} disabled={disabled || (!configError && (!config?.available || ids.length >= 1))} onClick={() => configError ? setRetry(n => n + 1) : input.current?.click()}><ImagePlus size={20}/></button>
    </div>
  </div>;
}
