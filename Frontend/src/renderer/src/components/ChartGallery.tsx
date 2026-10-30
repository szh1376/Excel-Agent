import { memo, useEffect, useState } from 'react'

import { chartUrl } from '../api'
import type { ChartRef } from '../store'

interface Props {
  charts: ChartRef[]
  /** 紧凑模式用于工作流面板的 Agent 卡片 */
  compact?: boolean
}

/** 大图弹层：点击缩略图后全屏查看。 */
function Lightbox({ chart, onClose }: { chart: ChartRef; onClose: () => void }): React.JSX.Element {
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="lightbox" onClick={onClose}>
      <div className="lightbox-inner" onClick={(event) => event.stopPropagation()}>
        <div className="lightbox-head">
          <span>{chart.title}</span>
          {chart.chartType && <span className="chart-type">{chart.chartType}</span>}
          <span className="spacer" />
          <a className="lightbox-open" href={chartUrl(chart.filename)} target="_blank" rel="noreferrer">
            用系统程序打开
          </a>
          <button className="icon-btn" onClick={onClose} title="关闭（Esc）">
            ✕
          </button>
        </div>
        <img src={chartUrl(chart.filename)} alt={chart.title} />
      </div>
    </div>
  )
}

/** 统计图展示：缩略图网格，点击放大。 */
export const ChartGallery = memo(function ChartGallery({ charts, compact = false }: Props) {
  const [opened, setOpened] = useState<ChartRef | null>(null)

  if (charts.length === 0) return null

  return (
    <>
      <div className={`chart-gallery${compact ? ' compact' : ''}`}>
        {charts.map((chart) => (
          <figure key={chart.filename} className="chart-item">
            <button className="chart-thumb" onClick={() => setOpened(chart)} title="点击放大">
              {/* 不用 loading="lazy"：图在视口外时浏览器不发请求，
                  用户会以为图没生成出来；图表本来就少且不大，直接加载更好 */}
              <img src={chartUrl(chart.filename)} alt={chart.title} />
            </button>
            <figcaption>
              <span className="chart-title">{chart.title}</span>
              {chart.chartType && <span className="chart-type">{chart.chartType}</span>}
            </figcaption>
          </figure>
        ))}
      </div>
      {opened && <Lightbox chart={opened} onClose={() => setOpened(null)} />}
    </>
  )
})
