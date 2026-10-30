/**
 * Electron 主进程。
 *
 * 职责：
 * 1. 把 Python 后端当子进程拉起来（项目根目录下的 run_server.py）
 * 2. 轮询 /api/health 等它就绪，拿到真正的端口
 * 3. 通过 preload 把后端地址交给渲染进程
 * 4. 退出时收掉子进程，别留孤儿
 *
 * 后端不用固定端口：8765 被占用时 run_server.py 会自动换一个空闲端口，
 * 并把「ExcelAgent 后端已就绪 http://127.0.0.1:PORT」打到 stdout，主进程读这一行。
 */

import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { app, BrowserWindow, dialog, shell } from 'electron'

// 项目根目录：Frontend/out/main/index.js -> 上溯三级
const PROJECT_ROOT = resolve(__dirname, '..', '..', '..')

const READY_PATTERN = /ExcelAgent 后端已就绪\s+(http:\/\/\S+)/
const HEALTH_TIMEOUT_MS = 120_000
const HEALTH_INTERVAL_MS = 400

let backend: ChildProcess | null = null
let backendUrl = ''
let mainWindow: BrowserWindow | null = null

/** 找项目虚拟环境里的 Python 解释器。 */
function resolvePython(): string {
  const candidates = [
    process.env.EXCEL_AGENT_PYTHON ?? '',
    join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe'), // Windows
    join(PROJECT_ROOT, '.venv', 'bin', 'python') // macOS / Linux
  ].filter(Boolean)

  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  // 都找不到时退回 PATH 里的 python，启动会失败并给出可读的报错
  return 'python'
}

/** 轮询健康检查接口，直到后端可用或超时。 */
async function waitForHealth(url: string): Promise<boolean> {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS
  while (Date.now() < deadline) {
    if (backend && backend.exitCode !== null) return false
    try {
      const res = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(2000) })
      if (res.ok) return true
    } catch {
      // 后端还没起来，继续等
    }
    await new Promise((r) => setTimeout(r, HEALTH_INTERVAL_MS))
  }
  return false
}

/** 拉起 Python 后端并等它就绪，返回后端地址。 */
function startBackend(): Promise<string> {
  return new Promise((resolvePromise, reject) => {
    const python = resolvePython()
    const script = join(PROJECT_ROOT, 'run_server.py')

    if (!existsSync(script)) {
      reject(new Error(`找不到后端启动脚本：${script}`))
      return
    }

    console.log(`[backend] 启动：${python} ${script}`)
    backend = spawn(python, [script], {
      cwd: PROJECT_ROOT,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      windowsHide: true
    })

    let settled = false
    let stderrTail = ''

    const finish = (url: string): void => {
      if (settled) return
      settled = true
      backendUrl = url
      resolvePromise(url)
    }

    const fail = (error: Error): void => {
      if (settled) return
      settled = true
      reject(error)
    }

    // run_server.py 会把就绪地址打到 stdout
    backend.stdout?.on('data', (chunk: Buffer) => {
      const text = chunk.toString('utf8')
      process.stdout.write(`[backend] ${text}`)
      const match = text.match(READY_PATTERN)
      if (match) finish(match[1].replace(/\/+$/, ''))
    })

    backend.stderr?.on('data', (chunk: Buffer) => {
      const text = chunk.toString('utf8')
      stderrTail = (stderrTail + text).slice(-2000)
      process.stderr.write(`[backend] ${text}`)
    })

    backend.on('error', (error) => {
      fail(new Error(`无法启动 Python 后端（${python}）：${error.message}`))
    })

    backend.on('exit', (code) => {
      if (!settled) {
        fail(new Error(`Python 后端启动失败（退出码 ${code}）\n${stderrTail}`))
      } else {
        console.log(`[backend] 已退出，退出码 ${code}`)
      }
    })
  })
}

/** 收掉后端子进程。 */
function stopBackend(): void {
  if (!backend || backend.exitCode !== null) return
  console.log('[backend] 正在关闭')
  backend.kill()
  backend = null
}

/** 创建主窗口。 */
function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1000,
    minHeight: 640,
    show: false,
    // 与界面底色一致，避免启动瞬间闪出深色
    backgroundColor: '#f7f8fa',
    title: 'ExcelAgent',
    webPreferences: {
      preload: join(__dirname, '..', 'preload', 'index.js'),
      // 后端地址由主进程运行时注入，preload 读它
      additionalArguments: [`--backend-url=${backendUrl}`],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  mainWindow.once('ready-to-show', () => mainWindow?.show())

  // 外部链接用系统浏览器打开，不在应用里跳走
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  const devServer = process.env.ELECTRON_RENDERER_URL
  if (devServer) {
    void mainWindow.loadURL(devServer)
  } else {
    void mainWindow.loadFile(join(__dirname, '..', 'renderer', 'index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

app.whenReady().then(async () => {
  try {
    await startBackend()
    console.log(`[backend] 就绪：${backendUrl}`)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    console.error('[backend] 启动失败', error)
    dialog.showErrorBox('后端启动失败', `${message}\n\n请确认已安装依赖：\n  .venv\\Scripts\\python.exe -m pip install fastapi uvicorn websockets`)
    app.quit()
    return
  }

  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  app.quit()
})

// 不管是正常退出还是被强杀，都收掉 Python 子进程
app.on('before-quit', stopBackend)
process.on('exit', stopBackend)
process.on('SIGINT', () => {
  stopBackend()
  process.exit(0)
})
