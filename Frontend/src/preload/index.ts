/**
 * preload：把主进程定下来的后端地址安全地交给渲染进程。
 *
 * 渲染进程开启了 contextIsolation，拿不到进程信息，
 * 所以在这里用 contextBridge 暴露一个只读的 window.__EXCEL_AGENT__。
 */

import { contextBridge } from 'electron'

const PREFIX = '--backend-url='

/** 从主进程附加的启动参数里取后端地址。 */
function readBackendUrl(): string {
  const arg = process.argv.find((item) => item.startsWith(PREFIX))
  if (arg) {
    const url = arg.slice(PREFIX.length).replace(/\/+$/, '')
    if (url) return url
  }
  // 兜底：直接用 run_server.py 的默认端口（浏览器里调试时也走这里）
  return 'http://127.0.0.1:8765'
}

const backendUrl = readBackendUrl()

contextBridge.exposeInMainWorld('__EXCEL_AGENT__', {
  backendUrl,
  wsUrl: backendUrl.replace(/^http/, 'ws') + '/ws'
})
