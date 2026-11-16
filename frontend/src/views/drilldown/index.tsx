import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/drilldown'
const FILTER_PRESET_ENDPOINT = '/api/filterpreset'
const columns = [
  "路径编号",
  "起始维度",
  "下钻层级序列",
  "每层聚合粒度",
  "默认度量",
  "路径深度",
  "缓存秒数",
  "数据新鲜度"
]
const actions = [
  "提交路径评审",
  "启用路径",
  "归档路径"
]
const statuses = [
  "草稿",
  "路径评审中",
  "已启用",
  "已归档"
]
const stats = [
  {
    "label": "下钻路径总数",
    "value": 0
  },
  {
    "label": "已启用路径",
    "value": 0
  },
  {
    "label": "评审中路径",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type SnapshotOption = { id: number; 快照编号: string; 预设编号: string; 预设名称: string }
type SnapshotResult = {
  快照编号?: string
  有效条件?: { 维度: string; 操作符: string; 值: unknown }[]
  失效维度?: { 维度: string }[]
  显示列?: string[]
  时间范围?: string
}

export default function DrilldownView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [snapshots, setSnapshots] = useState<SnapshotOption[]>([])
  const [selectedSnapshot, setSelectedSnapshot] = useState('')
  const [snapshotResult, setSnapshotResult] = useState<SnapshotResult | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  useEffect(() => {
    request(`${FILTER_PRESET_ENDPOINT}/snapshots`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => setSnapshots(payload?.items ?? []))
      .catch(() => undefined)
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('下钻路径列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function attachSnapshot(row: Row) {
    setErrorMessage('')
    setSnapshotResult(null)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/snapshot`, {
        method: 'POST',
        body: JSON.stringify({ values: { snapshot_id: Number(selectedSnapshot) } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '筛选条件快照挂载失败')
      }
      setSnapshotResult(payload.entry ?? null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选条件快照挂载失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('多维下钻动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻操作失败')
    }
  }

  return (
    <section className="page" data-module="drilldown">
      <header className="page-head">
        <div>
          <h2>多维下钻管理</h2>
          <p className="page-desc">定义多维度数据下钻的分析路径，从高层汇总到明细的逐层探索。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('下钻路径登记入口尚未接入审批流')}>
            登记下钻路径
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出多维下钻清单
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

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>筛选条件快照</span>
          <select value={selectedSnapshot} onChange={(event) => setSelectedSnapshot(event.target.value)}>
            <option value="">选择快照</option>
            {snapshots.map((item) => (
              <option key={item.id} value={item.id}>{item.快照编号} · {item.预设编号} {item.预设名称}</option>
            ))}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>筛选快照</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.筛选快照编号 ?? '—'}</td>
              <td className="row-actions">
                <button
                  className="link"
                  type="button"
                  disabled={!selectedSnapshot}
                  title={selectedSnapshot ? '把快照挂到该路径' : '先选择筛选条件快照'}
                  onClick={() => void attachSnapshot(row)}
                >
                  挂载快照
                </button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无多维下钻数据，可先登记下钻路径</td>
            </tr>
          )}
        </tbody>
      </table>

      {snapshotResult && (
        <section className="panel">
          <header className="panel-head">
            <h3>已挂载的筛选条件快照：{snapshotResult.快照编号}</h3>
            <button className="btn ghost" type="button" onClick={() => setSnapshotResult(null)}>关闭</button>
          </header>
          <dl className="detail-grid">
            <div><dt>时间范围</dt><dd>{snapshotResult.时间范围 ?? '—'}</dd></div>
            <div><dt>显示列</dt><dd>{(snapshotResult.显示列 ?? []).join('、') || '—'}</dd></div>
            <div><dt>失效维度</dt><dd>{(snapshotResult.失效维度 ?? []).map((item) => item.维度).join('、') || '无'}</dd></div>
          </dl>
          <h4>套用条件（{(snapshotResult.有效条件 ?? []).length} 条）</h4>
          <table className="data-table compact">
            <thead><tr><th>维度</th><th>操作符</th><th>值</th></tr></thead>
            <tbody>
              {(snapshotResult.有效条件 ?? []).map((condition, index) => (
                <tr key={`${condition.维度}-${index}`}>
                  <td>{condition.维度}</td>
                  <td>{condition.操作符}</td>
                  <td>{Array.isArray(condition.值) ? condition.值.join('、') : String(condition.值 ?? '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条多维下钻记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
