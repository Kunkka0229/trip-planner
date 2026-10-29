// P9：攻略知识库相关的解析——引用出处、闭馆提醒
// 后端只给了纯文字（给模型看的格式），前端按固定格式把它解析回结构化数据

/** 一条引用出处，对应 search_travel_guide 结果里的一个 [n] */
export interface GuideSource {
  n: number
  title: string // 文档标题，例如"故宫博物院游览指南"
  section: string // 小节标题，例如"开放时间与闭馆"
  updatedAt: string
  text: string // 原文
}

// 后端 format_hits 的格式：[1] 《标题》小节（更新于 2026-05-01）\n正文
// m：多行模式，^ $ 匹配每一行的开头结尾；g：找出全部
const SOURCE_HEADER = /^\[(\d+)\] 《(.+?)》(.*?)（更新于 (.+?)）$/gm

/**
 * 解析 search_travel_guide 的结果
 * 正文里可能有空行，所以不能简单按 "\n\n" 切：用标题行定位，两个标题行之间的就是上一条的正文
 * "知识库里没有找到相关内容。"这类没有标题行的结果，返回空数组
 */
export function parseSources(content: string): GuideSource[] {
  const headers = [...content.matchAll(SOURCE_HEADER)]
  return headers.map((m, i) => {
    const start = m.index! + m[0].length
    const end = i + 1 < headers.length ? headers[i + 1].index! : content.length
    return {
      n: Number(m[1]),
      title: m[2],
      section: m[3],
      updatedAt: m[4],
      text: content.slice(start, end).trim(),
    }
  })
}

/** 回复文字切成的片段：普通文字，或者一个引用上标 */
export type ReplySegment = { type: 'text'; text: string } | { type: 'cite'; source: GuideSource }

/**
 * 把回复里的 [1] [2] 切出来，换成引用片段
 * 找不到对应出处的编号（比如刷新页面后工具结果没了，或者模型编了一个编号）按原文显示
 */
export function splitCitations(content: string, sources: GuideSource[] | undefined): ReplySegment[] {
  if (!sources?.length) return [{ type: 'text', text: content }]
  const byNumber = new Map(sources.map((s) => [s.n, s]))
  const segments: ReplySegment[] = []
  let last = 0
  for (const m of content.matchAll(/\[(\d+)\]/g)) {
    const source = byNumber.get(Number(m[1]))
    if (!source) continue
    if (m.index! > last) segments.push({ type: 'text', text: content.slice(last, m.index) })
    segments.push({ type: 'cite', source })
    last = m.index! + m[0].length
  }
  if (last < content.length) segments.push({ type: 'text', text: content.slice(last) })
  return segments
}

/** 一条闭馆冲突：第 day 天（从 1 开始）的 name 在闭馆日 */
export interface ClosureWarning {
  day: number
  name: string
  when: string // 括号里的日期和星期，例如"2026-11-02 周一"
}

// 后端 closure_warnings 的格式：第 1 天（2026-11-02 周一）故宫博物院闭馆
// 多条之间用"；"连接，名字用非贪婪的 .+? 匹配到第一个"闭馆"为止
const CLOSURE = /第 (\d+) 天（([^）]*)）(.+?)闭馆/g

/** 从生成结果的 message、修改工具的返回里找出闭馆冲突 */
export function parseClosures(text: string): ClosureWarning[] {
  return [...text.matchAll(CLOSURE)].map((m) => ({ day: Number(m[1]), when: m[2], name: m[3] }))
}
