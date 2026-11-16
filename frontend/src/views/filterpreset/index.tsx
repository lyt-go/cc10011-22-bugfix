import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/filterpreset'
const columns = [
  "预设编号",
  "预设名称",
  "维度条件组",
  "时间范围",
  "排序规则",
  "显示列",
  "共享状态",
  "使用次数"
]
const actions = [
  "提交预设评审",
  "发布预设",
  "废弃预设"
]
const allowedActions: Record<string, string[]> = {
  草稿: ["提交预设评审", "废弃预设"],
  预设评审中: ["发布预设", "废弃预设"],
  已发布: ["废弃预设"],
  已废弃: [],
}
const statuses = [
  "草稿",
  "预设评审中",
  "已发布",
  "已废弃"
]
const shareStates = [
  "未共享",
  "团队共享"
]

type Row = Record<string, string | number | null>
type Condition = { 维度: string; 操作符: string; 值: unknown }
type Snapshot = Record<string, unknown> & { id?: number; 快照编号?: string; 使用次数?: number }
type Detail = Row & {
  解析条件?: Condition[]
  时间范围解析?: Record<string, string> | null
  显示列清单?: string[]
  最新快照?: Snapshot | null
  使用记录?: Record<string, unknown>[]
  可套用?: boolean
}

const emptyForm = {
  预设编号: '',
  预设名称: '',
  维度条件组: '区域=华东;渠道=线上',
  时间范围: '',
  排序规则: '',
  显示列: '',
  共享状态: '未共享',
}

/**
 * 同一个预设在一小段时间窗口内复用同一个幂等键：
 * 重复点击、React 重复渲染或页面刷新都不会把使用次数加多次；
 * 超过窗口后再次套用才算一次新的使用（生成新快照）。
 */
const APPLY_DEDUPE_WINDOW_MS = 10 * 60 * 1000

function idempotencyKey(presetId: number): string {
  const storageKey = `filterpreset:apply:${presetId}`
  const now = Date.now()
  try {
    const raw = window.sessionStorage.getItem(storageKey)
    if (raw) {
      const saved = JSON.parse(raw) as { key?: string; ts?: number }
      if (saved.key && saved.ts && now - saved.ts < APPLY_DEDUPE_WINDOW_MS) {
        return saved.key
      }
    }
  } catch {
    // 会话存储不可用时退化为每次请求新键，不影响后端幂等逻辑。
  }
  const created = `${presetId}-${now}-${Math.random().toString(16).slice(2)}`
  window.sessionStorage.setItem(storageKey, JSON.stringify({ key: created, ts: now }))
  return created
}

export default function FilterpresetView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [keyword, setKeyword] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [form, setForm] = useState<Record<string, string>>(emptyForm)
  const [showForm, setShowForm] = useState(false)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)

  const stats = useMemo(() => {
    const published = rows.filter((row) => row.status === '已发布').length
    const reviewing = rows.filter((row) => row.status === '预设评审中').length
    return [
      { label: '匹配预设总数', value: total },
      { label: '本页已发布', value: published },
      { label: '本页评审中', value: reviewing },
    ]
  }, [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (keyword.trim()) query.set('keyword', keyword.trim())
    if (statusFilter) query.set('status', statusFilter)
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('预设模板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选预设列表读取失败')
    }
  }, [keyword, statusFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openDetail(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('预设模板详情读取失败')
      }
      setDetail((await response.json()) as Detail)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选预设详情读取失败')
    }
  }

  async function submitForm(event: React.FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: form }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '预设模板保存失败')
      }
      setNotice(payload.message ?? '预设模板已登记')
      setShowForm(false)
      setForm(emptyForm)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选预设保存失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '筛选预设动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '操作已完成')
      await reload()
      if (detail && detail.id === row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选预设操作失败')
    }
  }

  async function applyPreset(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/apply`, {
        method: 'POST',
        body: JSON.stringify({ values: { request_id: idempotencyKey(Number(row.id)) } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '预设套用失败')
      }
      setSnapshot(payload.entry ?? null)
      setNotice(payload.message ?? '预设已套用')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选预设套用失败')
    }
  }

  async function updateShareStatus(value: string) {
    if (!detail) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${detail.id}`, {
        method: 'PUT',
        body: JSON.stringify({ values: { 共享状态: value } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '共享状态保存失败')
      }
      setNotice('共享状态已更新')
      await reload()
      await openDetail(detail as Row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '共享状态保存失败')
    }
  }

  return (
    <section className="page" data-module="filterpreset">
      <header className="page-head">
        <div>
          <h2>筛选预设管理</h2>
          <p className="page-desc">管理分析查询的筛选条件预设，支持一键套用和团队共享；套用会生成可追溯的筛选条件快照。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowForm((prev) => !prev)}>
            {showForm ? '收起登记表单' : '登记预设模板'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出筛选预设清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      {showForm && (
        <form className="panel" onSubmit={(event) => void submitForm(event)}>
          <h3>登记预设模板</h3>
          <div className="form-grid">
            <label className="filter-item">
              <span>预设编号</span>
              <input value={form.预设编号} onChange={(event) => setForm((prev) => ({ ...prev, 预设编号: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>预设名称</span>
              <input value={form.预设名称} onChange={(event) => setForm((prev) => ({ ...prev, 预设名称: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>时间范围</span>
              <input placeholder="如 2026-09-01~2026-09-30 或 近30天" value={form.时间范围} onChange={(event) => setForm((prev) => ({ ...prev, 时间范围: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>排序规则（显示顺序）</span>
              <input value={form.排序规则} onChange={(event) => setForm((prev) => ({ ...prev, 排序规则: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>显示列</span>
              <input placeholder="逗号分隔，如 日期,区域,指标值" value={form.显示列} onChange={(event) => setForm((prev) => ({ ...prev, 显示列: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>共享状态</span>
              <select value={form.共享状态} onChange={(event) => setForm((prev) => ({ ...prev, 共享状态: event.target.value }))}>
                {shareStates.map((state) => <option key={state} value={state}>{state}</option>)}
              </select>
            </label>
          </div>
          <label className="filter-item block">
            <span>维度条件组（每行或分号分隔一个条件）</span>
            <textarea rows={3} value={form.维度条件组} onChange={(event) => setForm((prev) => ({ ...prev, 维度条件组: event.target.value }))} />
          </label>
          <div className="form-actions">
            <button className="btn primary" type="submit">保存预设</button>
            <button className="btn ghost" type="button" onClick={() => setForm(emptyForm)}>重置</button>
          </div>
        </form>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>预设编号 / 名称</span>
          <input value={keyword} placeholder="按预设编号或名称检索" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>发布状态</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setKeyword(''); setStatusFilter('') }}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>详情</button>
                <button
                  className="link"
                  type="button"
                  disabled={row.status !== '已发布'}
                  title={row.status !== '已发布' ? '只有已发布预设才能套用' : '套用并生成筛选条件快照'}
                  onClick={() => void applyPreset(row)}
                >
                  套用预设
                </button>
                {actions.map((action) => (
                  <button
                    key={action}
                    className="link"
                    type="button"
                    disabled={!(allowedActions[String(row.status)] ?? []).includes(action)}
                    onClick={() => void runAction(action, row)}
                  >
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无筛选预设数据，可先登记预设模板</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="panel">
          <header className="panel-head">
            <h3>预设详情：{detail.预设编号} · {detail.预设名称}</h3>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>
          <dl className="detail-grid">
            <div><dt>发布状态</dt><dd>{detail.发布状态 ?? detail.status}</dd></div>
            <div><dt>版本</dt><dd>{detail.版本 ?? '—'}</dd></div>
            <div><dt>时间范围</dt><dd>{detail.时间范围 || '—'}</dd></div>
            <div><dt>排序规则</dt><dd>{detail.排序规则 || '—'}</dd></div>
            <div><dt>显示列</dt><dd>{(detail.显示列清单 ?? []).join('、') || '—'}</dd></div>
            <div><dt>使用次数</dt><dd>{detail.使用次数 ?? 0}</dd></div>
            <div>
              <dt>共享状态</dt>
              <dd>
                <select
                  value={String(detail.共享状态 ?? '未共享')}
                  disabled={detail.status === '已废弃'}
                  onChange={(event) => void updateShareStatus(event.target.value)}
                >
                  {shareStates.map((state) => <option key={state} value={state}>{state}</option>)}
                </select>
              </dd>
            </div>
            <div><dt>可套用</dt><dd>{detail.可套用 ? '是' : '否（仅已发布共享预设可套用）'}</dd></div>
          </dl>
          <h4>解析后的维度条件（{detail.解析条件?.length ?? 0} 条）</h4>
          <table className="data-table compact">
            <thead><tr><th>维度</th><th>操作符</th><th>值</th></tr></thead>
            <tbody>
              {(detail.解析条件 ?? []).map((condition, index) => (
                <tr key={`${condition.维度}-${index}`}>
                  <td>{condition.维度}</td>
                  <td>{condition.操作符}</td>
                  <td>{Array.isArray(condition.值) ? condition.值.join('、') : String(condition.值 ?? '')}</td>
                </tr>
              ))}
              {(detail.解析条件 ?? []).length === 0 && (
                <tr><td className="empty-state" colSpan={3}>维度条件组为空或无法解析</td></tr>
              )}
            </tbody>
          </table>
          <h4>最近一次筛选条件快照</h4>
          {detail.最新快照 ? (
            <SnapshotView snapshot={detail.最新快照} />
          ) : (
            <p className="muted">该预设尚未被套用，暂无快照。</p>
          )}
          <h4>使用记录（{detail.使用记录?.length ?? 0} 条）</h4>
          <table className="data-table compact">
            <thead><tr><th>快照编号</th><th>预设版本</th><th>操作人</th><th>幂等键</th><th>使用时间</th></tr></thead>
            <tbody>
              {(detail.使用记录 ?? []).map((record) => (
                <tr key={String(record.id)}>
                  <td>{String(record.快照编号 ?? '—')}</td>
                  <td>{String(record.预设版本 ?? '—')}</td>
                  <td>{String(record.操作人 ?? '—')}</td>
                  <td>{String(record.幂等键 ?? '—')}</td>
                  <td>{String(record.使用时间 ?? '—')}</td>
                </tr>
              ))}
              {(detail.使用记录 ?? []).length === 0 && (
                <tr><td className="empty-state" colSpan={5}>暂无使用记录</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {snapshot && (
        <section className="panel">
          <header className="panel-head">
            <h3>本次套用生成的筛选条件快照</h3>
            <button className="btn ghost" type="button" onClick={() => setSnapshot(null)}>关闭</button>
          </header>
          <SnapshotView snapshot={snapshot} />
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条筛选预设记录</span>
        <span>
          {notice && <span className="notice-text">{notice}　</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}

function SnapshotView({ snapshot }: { snapshot: Snapshot }) {
  const conditions = (snapshot.有效条件 ?? snapshot.解析条件 ?? []) as Condition[]
  const invalid = (snapshot.失效维度 ?? []) as Record<string, unknown>[]
  const invalidColumns = (snapshot.失效显示列 ?? []) as string[]
  return (
    <div className="snapshot">
      <dl className="detail-grid">
        <div><dt>快照编号</dt><dd>{String(snapshot.快照编号 ?? '—')}</dd></div>
        <div><dt>预设版本</dt><dd>{String(snapshot.预设版本 ?? '—')}</dd></div>
        <div><dt>时间范围</dt><dd>{String(snapshot.时间范围 ?? '—')}</dd></div>
        <div><dt>排序</dt><dd>{String(snapshot.排序规则 || '默认排序')}</dd></div>
        <div><dt>显示列</dt><dd>{((snapshot.显示列 ?? []) as string[]).join('、') || '—'}</dd></div>
        <div><dt>累计使用次数</dt><dd>{String(snapshot.使用次数 ?? 0)}</dd></div>
      </dl>
      <h4>查询条件（{conditions.length} 条，全部保留）</h4>
      <table className="data-table compact">
        <thead><tr><th>维度</th><th>操作符</th><th>值</th></tr></thead>
        <tbody>
          {conditions.map((condition, index) => (
            <tr key={`${condition.维度}-${index}`}>
              <td>{condition.维度}</td>
              <td>{condition.操作符}</td>
              <td>{Array.isArray(condition.值) ? condition.值.join('、') : String(condition.值 ?? '')}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className={invalid.length > 0 ? 'error-text' : 'muted'}>
        失效维度：{invalid.length > 0 ? invalid.map((item) => String(item.维度)).join('、') : '无'}
        {invalidColumns.length > 0 && `；失效显示列：${invalidColumns.join('、')}`}
      </p>
    </div>
  )
}
