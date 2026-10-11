// 给 import.meta.env 里的自定义变量加上类型，写 import.meta.env.VITE_ 时会有自动补全
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_AMAP_JS_KEY?: string
  readonly VITE_AMAP_SECURITY_CODE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

// 高德 JS API 的安全密钥要挂在 window 上，这里声明一下，TS 才不会报错
interface Window {
  _AMapSecurityConfig?: { securityJsCode: string }
}
