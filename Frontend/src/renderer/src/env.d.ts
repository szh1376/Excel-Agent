export {}

declare global {
  interface Window {
    /** 由 preload 注入：后端地址与 WebSocket 地址。 */
    __EXCEL_AGENT__?: {
      backendUrl: string
      wsUrl: string
    }
  }
}
